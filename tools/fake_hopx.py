"""A local imitation of hopx.psi.ch and a cluster behind it, for testing Hoppla.

Needs paramiko (pip install paramiko); Hoppla itself does not.

    python tools/fake_hopx.py            # hop on :2022, "Ra" on :2023, web on :8088

What it imitates (from https://www.psi.ch/computing/ssh-hop-ng and real logs):
  * login by keyboard-interactive: "Password:" then "Verification code:"
  * tunnels only work while an interactive "setup connection" (shell with a pty)
    is open; before that every forward fails with "connect failed: open failed"
  * only SSH to known clusters (ra.psi.ch:22) is allowed
  * one setup connection per user; a second one is refused
  * repeated failed logins block the client address; frequent logins are refused
Behind it, "ra.psi.ch" accepts password logins and lets forwards reach a small
web server that stands in for every internal web page (e.g. jupytera.psi.ch).
"""

from __future__ import annotations

import argparse
import http.server
import os
import socket
import socketserver
import threading
import time

import paramiko

USER, PASSWORD, CODE = "tester", "secret", "123456"
BANNER = ("#" * 60 + "\r\n##  FAKE hopx: authenticate with MFA  ##\r\n" + "#" * 60 + "\r\n")


class Limits:
    """Failed-login blocking and login frequency limit, per client address."""

    def __init__(self, max_failures=3, min_interval=5.0):
        self.failures: dict[str, int] = {}
        self.last_login: dict[str, float] = {}
        self.max_failures, self.min_interval = max_failures, min_interval
        self.lock = threading.Lock()

    def refuse(self, ip: str) -> str | None:
        with self.lock:
            if self.failures.get(ip, 0) >= self.max_failures:
                return "address blocked after failed logins"
            if time.time() - self.last_login.get(ip, 0) < self.min_interval:
                return "too many logins"
            self.last_login[ip] = time.time()
        return None

    def failed(self, ip: str) -> None:
        with self.lock:
            self.failures[ip] = self.failures.get(ip, 0) + 1

    def ok(self, ip: str) -> None:
        with self.lock:
            self.failures[ip] = 0


def bridge(chan, host: str, port: int) -> None:
    try:
        sock = socket.create_connection((host, port), timeout=5)
    except OSError:
        chan.close()
        return
    sock.settimeout(None)  # the timeout was only for connecting; idle tunnels must stay open

    done = []

    def pump(src, dst, end_write):
        try:
            while True:
                data = src.recv(32768)
                if not data:
                    break
                dst.sendall(data)
        except OSError:
            pass
        try:
            end_write()  # half-close: pass EOF on, the other direction may still be busy (like sshd)
        except OSError:
            pass
        done.append(1)
        if len(done) == 2:
            for x in (chan, sock):
                try:
                    x.close()
                except OSError:
                    pass
    threading.Thread(target=pump, args=(chan, sock, lambda: sock.shutdown(socket.SHUT_WR)), daemon=True).start()
    threading.Thread(target=pump, args=(sock, chan, chan.shutdown_write), daemon=True).start()


class HopServer(paramiko.ServerInterface):
    sessions: set[str] = set()  # users with an open setup connection

    def __init__(self, ip, limits: Limits, targets: dict, log):
        self.ip, self.limits, self.targets, self.log = ip, limits, targets, log
        self.stage = 0
        self.pty = False
        self.setup = threading.Event()
        self.pending: dict[int, tuple[str, int]] = {}
        self.user = None

    def get_allowed_auths(self, username):
        return "keyboard-interactive"

    def check_auth_interactive(self, username, submethods):
        self.user, self.stage = username, 1
        return paramiko.InteractiveQuery("", "", ("Password: ", False))

    def check_auth_interactive_response(self, responses):
        answer = responses[0] if responses else ""
        if self.stage == 1 and self.user == USER and answer == PASSWORD:
            self.stage = 2
            return paramiko.InteractiveQuery("", "", ("Verification code: ", True))
        if self.stage == 2 and answer == CODE:
            self.limits.ok(self.ip)
            self.log(f"hop: {self.user} logged in")
            return paramiko.AUTH_SUCCESSFUL
        self.limits.failed(self.ip)
        self.log(f"hop: login failed ({self.limits.failures[self.ip]} failures from {self.ip})")
        return paramiko.AUTH_FAILED

    def check_channel_request(self, kind, chanid):
        return paramiko.OPEN_SUCCEEDED if kind == "session" else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    def check_channel_pty_request(self, *args):
        self.pty = True
        return True

    def check_channel_shell_request(self, channel):
        if not self.pty:  # hopx needs a real terminal for the setup connection
            return False
        if self.user in HopServer.sessions:
            channel.send("You already have an open setup connection. Closing.\r\n")
            return False
        HopServer.sessions.add(self.user)
        self.setup.set()
        self.log("hop: setup connection open")
        threading.Thread(target=self._shell, args=(channel,), daemon=True).start()
        return True

    def _shell(self, chan):
        chan.send(BANNER + "Setup connection established. Keep this session open.\r\n")
        try:
            while chan.recv(1024):
                pass
        except OSError:
            pass
        finally:
            HopServer.sessions.discard(self.user)
            self.setup.clear()
            self.log("hop: setup connection closed")

    def check_channel_direct_tcpip_request(self, chanid, origin, destination):
        host, port = destination
        if not self.setup.is_set():
            self.log(f"hop: refused {host}:{port} (no setup connection)")
            return paramiko.OPEN_FAILED_CONNECT_FAILED
        if (host, port) not in self.targets:
            self.log(f"hop: refused {host}:{port} (not allowed)")
            return paramiko.OPEN_FAILED_CONNECT_FAILED
        self.pending[chanid] = self.targets[(host, port)]
        return paramiko.OPEN_SUCCEEDED


class RaServer(paramiko.ServerInterface):
    def __init__(self, web_port, log):
        self.web_port, self.log = web_port, log
        self.pending: dict[int, tuple[str, int]] = {}

    def get_allowed_auths(self, username):
        return "password"

    def check_auth_password(self, username, password):
        ok = username == USER and password == PASSWORD
        self.log(f"ra: login {'ok' if ok else 'failed'}")
        return paramiko.AUTH_SUCCESSFUL if ok else paramiko.AUTH_FAILED

    def check_channel_request(self, kind, chanid):
        return paramiko.OPEN_SUCCEEDED if kind == "session" else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    def check_channel_direct_tcpip_request(self, chanid, origin, destination):
        self.log(f"ra: forward to {destination[0]}:{destination[1]}")
        self.pending[chanid] = ("127.0.0.1", self.web_port)  # every internal page is our web server
        return paramiko.OPEN_SUCCEEDED


def serve(port, make_server, host_key, limits: Limits | None, log):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", port))
    sock.listen(50)

    def handle(client, addr):
        if limits:
            why = limits.refuse(addr[0])
            if why:
                log(f"hop: connection from {addr[0]} refused: {why}")
                client.close()
                return
        t = paramiko.Transport(client)
        t.add_server_key(host_key)
        server = make_server(addr[0])
        try:
            t.start_server(server=server)
        except (paramiko.SSHException, EOFError):
            return
        while t.is_active():
            chan = t.accept(1)
            if chan is None:
                continue
            target = server.pending.pop(chan.get_id(), None)
            if target:
                bridge(chan, *target)

    def loop():
        while True:
            client, addr = sock.accept()
            threading.Thread(target=handle, args=(client, addr), daemon=True).start()
    threading.Thread(target=loop, daemon=True).start()


class Page(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = f"Hello from inside PSI ({self.headers.get('Host')}{self.path})\n".encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def host_key(name: str) -> paramiko.PKey:
    """A key kept on disk, like a real server's: clients notice when it changes."""
    path = os.path.join(os.path.expanduser("~/.cache/fake_hopx"), name + ".key")
    if os.path.exists(path):
        return paramiko.RSAKey(filename=path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    key = paramiko.RSAKey.generate(2048)
    key.write_private_key_file(path)
    return key


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--hop-port", type=int, default=2022)
    ap.add_argument("--ra-port", type=int, default=2023)
    ap.add_argument("--web-port", type=int, default=8088)
    ap.add_argument("--min-interval", type=float, default=5.0, help="seconds between logins per address")
    a = ap.parse_args()

    def log(msg):
        print(time.strftime("%H:%M:%S"), msg, flush=True)

    socketserver.ThreadingTCPServer.allow_reuse_address = True  # quick restarts
    web = socketserver.ThreadingTCPServer(("127.0.0.1", a.web_port), Page)
    threading.Thread(target=web.serve_forever, daemon=True).start()
    limits = Limits(min_interval=a.min_interval)
    targets = {("ra.psi.ch", 22): ("127.0.0.1", a.ra_port), ("ra", 22): ("127.0.0.1", a.ra_port)}
    serve(a.hop_port, lambda ip: HopServer(ip, limits, targets, log), host_key("hop"), limits, log)
    serve(a.ra_port, lambda ip: RaServer(a.web_port, log), host_key("ra"), None, log)
    log(f"fake hopx on 127.0.0.1:{a.hop_port} (user {USER}, password {PASSWORD}, code {CODE}); "
        f"ra.psi.ch -> :{a.ra_port}; web pages -> :{a.web_port}")
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
