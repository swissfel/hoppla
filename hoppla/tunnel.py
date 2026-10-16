"""Building ssh command lines and running/monitoring the tunnel session.

hopx is a proxy-only gateway: an interactive "setup connection" (password + MFA)
must stay open, and only one is allowed per user. It carries local forwards to
the SSH port of internal hosts (e.g. Ra). Everything else -- SOCKS proxies and
port forwards -- runs on a second connection to such a host, made through that
forward (like ssh -J). External users may only reach clusters by SSH, and hopx
rate-limits logins, so we log in only on request and never probe the tunnels.

ssh never reads the user's ssh config files (-F none) and keeps its own
known_hosts file, so the tunnel behaves the same on every computer.

Two login modes:
  app       ssh runs hidden; password/OTP prompts appear as dialogs of this
            program (SSH_ASKPASS, needs OpenSSH >= 8.4).
  terminal  ssh runs in a visible terminal window and asks there.
"""

from __future__ import annotations

import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from .config import WINDOWS, Config, Service, config_dir, state_file
from .platforms import (PlatformError, detached_popen, find_ssh, kill_pid, open_in_terminal, pid_alive,
                        process_name, self_command)

KEEPALIVE = ["-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=3"]
LOCALHOST = "127.0.0.1"
ASKPASS_ENV = "HOPPLA_ASKPASS"


# ------------------------------------------------------------ commands ------
@dataclass
class Plan:
    gateway: list[Service] = field(default_factory=list)  # forwards on the gateway connection
    inner: dict[str, list[Service]] = field(default_factory=dict)  # ssh service id -> services through it

    @property
    def services(self) -> list[Service]:
        return self.gateway + [s for group in self.inner.values() for s in group]


def make_plan(cfg: Config, extra_ids=()) -> Plan:
    plan = Plan()
    for s in cfg.selected(extra_ids):
        if s.type != "ssh" and s.via != "gateway":
            plan.inner.setdefault(s.via, []).append(s)
        else:
            plan.gateway.append(s)
    return plan


def known_hosts_file() -> Path:
    return config_dir() / "known_hosts"


def _quoted(path: Path) -> str:
    return '"' + path.as_posix() + '"'  # ssh splits option values at spaces


def base_options(ssh: str) -> list[str]:
    """Options independent of anything in the user's ~/.ssh."""
    known_hosts_file().parent.mkdir(parents=True, exist_ok=True)
    return [ssh, "-F", "none", *KEEPALIVE,
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", f"UserKnownHostsFile={_quoted(known_hosts_file())}",
            # One attempt per connect: gateways block addresses after repeated failures.
            "-o", "NumberOfPasswordPrompts=1"]


def forward_args(services: list[Service]) -> list[str]:
    out = []
    for s in services:
        if s.type == "socks":
            out += ["-D", f"{LOCALHOST}:{s.local_port}"]
        elif s.type == "ssh":
            out += ["-L", f"{LOCALHOST}:{s.local_port}:{s.remote_host}:{s.ssh_port}"]
        else:
            out += ["-L", f"{LOCALHOST}:{s.local_port}:{s.remote_host}:{s.remote_port}"]
    return out


def gateway_command(cfg: Config, services: list[Service], ssh: str = "ssh") -> list[str]:
    """The interactive "setup connection" to hopx; it must stay open for any tunnel to work."""
    return [*base_options(ssh), "-tt", "-o", "ExitOnForwardFailure=yes",
            "-o", "PreferredAuthentications=keyboard-interactive",
            *forward_args(services), "-p", str(cfg.gateway_port), f"{cfg.user}@{cfg.gateway}"]


def via_localhost(cfg: Config, host: Service) -> list[str]:
    """Options to reach internal ssh service `host` through its local forward."""
    return ["-o", f"HostKeyAlias={host.remote_host}", "-p", str(host.local_port), f"{cfg.user}@{LOCALHOST}"]


def inner_command(cfg: Config, host: Service, services: list[Service], ssh: str = "ssh") -> list[str]:
    """Second connection, through the gateway's forward to `host`, carrying `services`."""
    return [*base_options(ssh), "-N", "-o", "ExitOnForwardFailure=yes",
            *forward_args(services), *via_localhost(cfg, host)]


def login_command(cfg: Config, host: Service, ssh: str = "ssh") -> list[str]:
    return [*base_options(ssh), *via_localhost(cfg, host)]


# --------------------------------------------------------- login mode -------
def ssh_version(ssh: str | None = None) -> tuple[int, int]:
    ssh = ssh or find_ssh()
    if not ssh:
        return (0, 0)
    try:
        out = subprocess.run([ssh, "-V"], capture_output=True, text=True, timeout=10,
                             creationflags=subprocess.CREATE_NO_WINDOW if WINDOWS else 0)
    except (OSError, subprocess.SubprocessError):
        return (0, 0)
    m = re.search(r"OpenSSH[^\d]*(\d+)\.(\d+)", out.stderr + out.stdout)
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)


def app_login_supported() -> bool:
    if WINDOWS and not getattr(sys, "frozen", False):
        return False  # ssh.exe cannot run a .py file as askpass helper
    return ssh_version() >= (8, 4)


def login_mode(cfg: Config) -> str:
    if cfg.login_mode == "terminal" or (cfg.login_mode == "app" and app_login_supported()):
        return cfg.login_mode
    return "app" if app_login_supported() else "terminal"


def askpass_program() -> str:
    """Executable ssh calls for each prompt; it re-enters this program in askpass mode."""
    if getattr(sys, "frozen", False):
        return sys.executable
    wrapper = config_dir() / "askpass.sh"
    wrapper.parent.mkdir(parents=True, exist_ok=True)
    import shlex
    wrapper.write_text(f'#!/bin/sh\nexec {shlex.join(self_command())} "$@"\n')
    wrapper.chmod(0o700)
    return str(wrapper)


def askpass_env(label: str) -> dict:
    env = dict(os.environ)
    env.update({"SSH_ASKPASS": askpass_program(), "SSH_ASKPASS_REQUIRE": "force",
                ASKPASS_ENV: "1", "HOPPLA_LOGIN_LABEL": label})
    return env


# ---------------------------------------------------------------- ports -----
# Never connect to our forwarded ports to test them: every connection makes the
# gateway open a channel to the target, and the gateway rate-limits.
def listening(port: int) -> bool:
    return not port_free(port)


def port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        if not WINDOWS:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((LOCALHOST, int(port)))
            return True
        except OSError:
            return False


# ---------------------------------------------------------------- state -----
def log_file() -> Path:
    return config_dir() / "session.log"


def result_file() -> Path:
    return config_dir() / "last-session.json"


def cancel_flag() -> Path:
    return config_dir() / "login-cancelled"


def read_state() -> dict:
    try:
        state = json.loads(state_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    pid = state.get("runner_pid", 0)
    if not pid_alive(pid):
        return {}
    # after a crash or reboot the pid may belong to another program now
    if state.get("runner_exe") and process_name(pid) not in ("", state["runner_exe"]):
        return {}
    return state


def write_state(state: dict) -> None:
    path = state_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state), encoding="utf-8")


def clear_state() -> None:
    try:
        state_file().unlink()
    except OSError:
        pass


def running_services() -> list[str]:
    return read_state().get("services", [])


def last_result() -> dict:
    try:
        return json.loads(result_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def stop() -> bool:
    state = read_state()
    if not state:
        clear_state()
        return False
    _write_result("Disconnected.", user_stopped=True)
    kill_pid(state["runner_pid"])  # first, so it does not report the ssh exit as an error
    for pid in state.get("ssh_pids", []):
        kill_pid(pid)
    clear_state()
    return True




# --------------------------------------------------------- diagnostics ------
LOGIN_FAILED = "Login failed: wrong user name, password or MFA code."
LOCKOUT_WARNING = ("\n\nCareful: after several failed logins the gateway blocks your internet address "
                   "for a while. Check your user name, and wait for a fresh code before trying again.")

ERRORS = [
    ("REMOTE HOST IDENTIFICATION HAS CHANGED", "The server's identity changed. This can be a security problem; "
                                               "contact PSI IT before connecting again."),
    ("Host key verification failed", "The server's identity could not be verified."),
    ("Permission denied", LOGIN_FAILED),
    ("Too many authentication failures", "Login failed: too many attempts."),
    ("Could not resolve hostname", "Cannot find the gateway. Are you connected to the internet?"),
    ("timed out", "No answer from the gateway. Check your internet connection."),
    ("Connection refused", "The gateway refused the connection."),
    ("Network is unreachable", "No network connection."),
    ("administratively prohibited", "The gateway does not allow one of the selected targets. "
                                    "Check host names and ports of the services."),
    ("open failed", "The gateway refused a connection to one of the selected targets. External users "
                    "can usually only reach clusters such as Ra by SSH; run proxies and forwards through Ra."),
    ("forwarding failed", "Setting up a port forward failed (port busy or target not allowed)."),
    ("cannot listen to port", "A local port is already in use by another program."),
    ("Timeout, server", "The connection was lost (no answer from the gateway)."),
    ("Broken pipe", "The connection was lost."),
    ("closed by remote host", "The gateway closed the connection. Only one hopx session per user is "
                              "allowed (is another one open?), and sessions end after 12 hours."),
    ("Connection reset", "The connection was lost."),
]


def explain(log_text: str) -> str:
    for needle, text in ERRORS:
        if needle.lower() in log_text.lower():
            return text
    lines = [ln for ln in log_text.strip().splitlines() if ln.strip()]
    return lines[-1] if lines else "The connection ended."


def _write_result(message: str, **extra) -> None:
    """Outcome of the last session; keeps the attempt time and failure count unless given."""
    prev = last_result()
    data = {"attempt": prev.get("attempt", 0), "login_failures": prev.get("login_failures", 0)}
    data.update({"message": message, "time": time.time(), **extra})
    try:
        result_file().parent.mkdir(parents=True, exist_ok=True)
        result_file().write_text(json.dumps(data), encoding="utf-8")
    except OSError:
        pass


# ------------------------------------------------------------ rate limit ----
MIN_INTERVAL = 30  # seconds between gateway logins
FAILURE_PAUSE = 120  # extra seconds per failed login in a row, capped below
MAX_PAUSE = 600


def cooldown_left() -> int:
    r = last_result()
    fails = r.get("login_failures", 0)
    wait = min(MAX_PAUSE, MIN_INTERVAL + FAILURE_PAUSE * fails)
    return max(0, int(r.get("attempt", 0) + wait - time.time()))


def cooldown_message(left: int) -> str:
    return (f"Please wait {left} seconds before connecting again.\n"
            "The gateway blocks internet addresses that log in too often.")


def _record_attempt() -> None:
    _write_result("", attempt=time.time())


# ------------------------------------------------------------- session ------
YES_NO = re.compile(r"\((?:y|yes)\s*/\s*(?:n|no)\)|\[(?:y|yes)\s*/\s*(?:n|no)\]", re.I)


def _wait_ports(proc: subprocess.Popen, ports: list[int], settle: float = 0) -> bool:
    while proc.poll() is None:
        if cancel_flag().exists():  # "Cancel" in a login dialog
            proc.terminate()
            return False
        if all(listening(p) for p in ports):
            time.sleep(settle)
            return proc.poll() is None
        time.sleep(0.5)
    return False


def _terminate(procs) -> None:
    for p in procs:
        if p.poll() is None:
            p.terminate()
    for p in procs:
        try:
            p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            p.kill()


def _print(*args) -> None:
    print(*args, flush=True)


def _sigterm(signum, frame):
    raise KeyboardInterrupt


def check(cfg: Config, plan: Plan) -> list[str]:
    problems = cfg.problems(plan.services)
    if not plan.services:
        problems.append("No services selected.")
    busy = [f"{s.local_port} ({s.name})" for s in plan.services if not port_free(s.local_port)]
    if busy:
        problems.append("Local ports already in use by another program or tunnel: " + ", ".join(busy))
    return problems


def ask_yes_no(question: str, label: str) -> bool:
    """Ask in a dialog (separate process, as for passwords)."""
    env = askpass_env(label)
    try:
        out = subprocess.run([askpass_program(), question], env=env, capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.SubprocessError):
        return False
    return out.stdout.strip().lower().startswith("y")


class Session:
    """One tunnel session. In background mode ssh output goes to session.log."""

    def __init__(self, cfg: Config, extra_ids=(), background: bool = False, out=_print):
        self.cfg, self.background, self.out = cfg, background, out
        self.plan = make_plan(cfg, extra_ids)
        self.ssh = find_ssh() or "ssh"
        self.procs: list[subprocess.Popen] = []
        self.log = None
        self.lock = threading.Lock()

    # -- processes
    def _popen(self, cmd: list[str], label: str, interactive: bool = False) -> subprocess.Popen:
        if not self.background:
            p = subprocess.Popen(cmd)  # terminal: the user sees and answers everything
            p.tail = deque(maxlen=0)
        else:
            kw = {"creationflags": subprocess.CREATE_NO_WINDOW} if WINDOWS else {"start_new_session": True}
            p = subprocess.Popen(cmd, stdin=subprocess.PIPE if interactive else subprocess.DEVNULL,
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=askpass_env(label), **kw)
            p.tail = deque(maxlen=200)
            threading.Thread(target=self._pump, args=(p, label, interactive), daemon=True).start()
        self.procs.append(p)
        self._save()
        return p

    def _pump(self, p: subprocess.Popen, label: str, interactive: bool) -> None:
        """Copy ssh output to the log; answer yes/no questions of the gateway with a dialog."""
        recent = ""
        while True:
            chunk = p.stdout.read1(4096) if hasattr(p.stdout, "read1") else p.stdout.read(1)
            if not chunk:
                break
            text = chunk.decode("utf-8", "replace")
            with self.lock:
                self.log.write(text)
                self.log.flush()
            p.tail.append(text)
            recent = (recent + text)[-600:]
            if interactive and YES_NO.search(recent):
                question = [ln for ln in recent.replace("\r", "").splitlines() if ln.strip()][-3:]
                recent = ""
                answer = ask_yes_no("\n".join(question) + "\n(yes/no)", label)
                try:
                    p.stdin.write(b"y\n" if answer else b"n\n")
                    p.stdin.flush()
                except OSError:
                    pass

    def _save(self) -> None:
        write_state({
            "runner_pid": os.getpid(),
            "runner_exe": process_name(os.getpid()),
            "ssh_pids": [p.pid for p in self.procs],
            "services": [s.id for s in self.plan.services],
            "mode": "app" if self.background else "terminal",
            "started": time.time(),
        })

    def _tail(self, p) -> str:
        return "".join(p.tail)

    # -- connection
    def _start_inner(self, host: Service, services: list[Service]) -> None:
        cfg = self.cfg
        names = ", ".join(s.name for s in services)
        if not self.background:
            self.out(f"\nConnecting to {host.remote_host} for: {names}")
            self.out(f"Log in to {host.remote_host} if asked (password or SSH key).")
        for attempt in range(3):  # the gateway may need a moment before it lets us through
            t0 = time.time()
            p = self._popen(inner_command(cfg, host, services, self.ssh), host.remote_host)
            if _wait_ports(p, [s.local_port for s in services]):
                return
            quick = time.time() - t0 < 8
            if not quick or "Permission denied" in self._tail(p):
                break
            time.sleep(3)
        self.out(f"!! Could not connect to {host.remote_host}; {names} not available.")

    def connect_once(self) -> bool:
        """Log in, start inner connections, block until the gateway session ends."""
        cfg = self.cfg
        if not self.background:
            self.out("=" * 70)
            self.out(f" Hoppla: connecting to {cfg.gateway} as {cfg.user}")
            self.out(" Enter your PSI password. Then approve the MFA request on your phone,")
            self.out(" or type the code when asked. Keep this window open while you work.")
            self.out("=" * 70)
        _record_attempt()
        gw = self._popen(gateway_command(cfg, self.plan.gateway, self.ssh), f"PSI gateway ({cfg.gateway})",
                         interactive=True)
        if not _wait_ports(gw, [s.local_port for s in self.plan.gateway], settle=2):
            return False
        _write_result("Connected.", connected=True, login_failures=0)
        for host_id, services in self.plan.inner.items():
            if gw.poll() is not None:
                break
            self._start_inner(cfg.service(host_id), services)
        if not self.background:
            _summary(cfg, self.plan, self.out)
        gw.wait()
        return True

    def run(self) -> int:
        problems = check(self.cfg, self.plan)
        if read_state():
            problems = ["A Hoppla connection is already running."]
        elif cooldown_left():
            problems = [cooldown_message(cooldown_left())]
        if problems:
            self.out("Cannot start:\n  " + "\n  ".join(problems))
            _write_result("\n".join(problems), failed=True)
            return 2
        if not WINDOWS:
            signal.signal(signal.SIGTERM, _sigterm)
        try:
            cancel_flag().unlink()
        except OSError:
            pass
        if self.background:
            self.log = open(log_file(), "w", encoding="utf-8", errors="replace")
        try:
            while True:
                was_up = self.connect_once()
                _terminate(self.procs)
                if self.background:
                    time.sleep(0.3)  # let the reader threads finish
                    text = "".join(self._tail(p) for p in self.procs)
                    self.procs = []
                    if cancel_flag().exists():
                        msg = "Login cancelled."
                    elif was_up:
                        msg = "The connection ended. " + explain(text)
                    else:
                        msg = explain(text)
                    failures = last_result().get("login_failures", 0) + 1 if msg == LOGIN_FAILED else 0
                    if failures >= 2:
                        msg += LOCKOUT_WARNING
                    _write_result(msg, failed=not was_up, login_failures=failures)
                    return 0 if was_up else 1
                self.procs = []
                self.out("\nConnection closed.")
                try:
                    input("Press Enter to reconnect, or close this window to quit. ")
                    while cooldown_left():  # never hammer the gateway
                        self.out(f"Waiting {cooldown_left()} s (the gateway limits login frequency)...")
                        time.sleep(min(10, cooldown_left() or 1))
                except EOFError:
                    return 0
        except KeyboardInterrupt:
            self.out("\nStopping.")
            return 0
        finally:
            _terminate(self.procs)
            clear_state()
            if self.log:
                self.log.close()


def run_session(cfg: Config, extra_ids=(), out=_print) -> int:
    """Interactive session in the current terminal."""
    if not find_ssh():
        from .platforms import ssh_install_hint
        out(ssh_install_hint())
        return 2
    return Session(cfg, extra_ids, background=False, out=out).run()


def _summary(cfg: Config, plan: Plan, out) -> None:
    out("\nConnected. Available on this computer:")
    for s in plan.services:
        if not listening(s.local_port):
            continue
        if s.type == "socks":
            what = f"SOCKS5 proxy  127.0.0.1:{s.local_port}"
        elif s.type == "ssh":
            what = f"ssh -p {s.local_port} {cfg.user}@127.0.0.1   ({s.remote_host})"
        else:
            what = f"127.0.0.1:{s.local_port} -> {s.target()}"
        out(f"  {s.name:32} {what}")
    out("\nPress Ctrl+C to disconnect.")


# ------------------------------------------------- start from GUI/icons -----
class TunnelError(RuntimeError):
    pass


def start(cfg: Config, extra_ids=()) -> str:
    """Start the session in the configured login mode; returns the mode used."""
    problems = check(cfg, make_plan(cfg, extra_ids))
    if problems:
        raise TunnelError("\n".join(problems))
    left = cooldown_left()
    if left:
        raise TunnelError(cooldown_message(left))
    mode = login_mode(cfg)
    args = []
    for sid in extra_ids:
        args += ["--with", sid]
    _write_result("")  # clear the previous outcome (attempt time and failures are kept)
    if mode == "app":
        proc = detached_popen(self_command("session", "--background", *args, gui=True))
        threading.Thread(target=proc.wait, daemon=True).start()  # reap it, no zombie
    else:
        try:
            open_in_terminal(self_command("session", *args), title="Hoppla")
        except PlatformError as e:
            raise TunnelError(str(e)) from e
    return mode


def ensure_service(cfg: Config, sid: str, timeout: float = 300, progress=None) -> Service:
    """Make sure service `sid` is reachable, starting the tunnel if needed."""
    service = cfg.service(sid)
    if listening(service.local_port) and sid in running_services():
        return service
    if read_state():
        if sid not in running_services():
            raise TunnelError(f"The running connection does not include '{service.name}'.\n"
                              "Disconnect and connect again with this service ticked.")
    else:
        start(cfg, [sid])
    t0 = time.time()
    started = False
    while time.time() < t0 + timeout:
        state = read_state()
        started = started or bool(state)
        if state and listening(service.local_port):
            return service
        if started and not state:
            raise TunnelError(last_result().get("message") or "The connection ended before it was ready.")
        if not started and time.time() > t0 + 30:
            raise TunnelError(last_result().get("message") or "The connection could not be started.")
        if progress:
            progress("Waiting for login...")
        time.sleep(1)
    raise TunnelError("Timed out waiting for the connection. Did the login succeed?")


def session_main(extra_ids, background: bool = False) -> int:
    from .config import load
    from .platforms import ensure_console

    if background:
        return Session(load(), extra_ids, background=True, out=lambda *a: None).run()
    ensure_console("Hoppla")
    code = run_session(load(), extra_ids)
    if code:
        try:
            input("\nPress Enter to close. ")
        except EOFError:
            pass
    sys.stdout.flush()
    return code
