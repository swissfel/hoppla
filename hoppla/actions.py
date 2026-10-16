"""Opening services (RDP, web pages, SSH logins) and the ~/.ssh/config block."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from .config import MACOS, WINDOWS, Config, Service, config_dir
from .platforms import detached_popen, find_ssh, open_in_terminal, open_path
from .tunnel import LOCALHOST, login_command

BEGIN = "# >>> hoppla managed block (edit in Hoppla, not here) >>>"
END = "# <<< hoppla managed block <<<"


def open_service(cfg: Config, s: Service) -> None:
    if s.type == "ssh":
        open_ssh_login(cfg, s)
    elif s.type == "forward" and s.open == "url":
        import webbrowser
        webbrowser.open(s.open_url() or f"http://localhost:{s.local_port}/")
    elif s.type == "forward" and s.open == "rdp":
        open_rdp(cfg, s)
    else:
        raise ValueError(f"'{s.name}' has no open action; connect your program to 127.0.0.1:{s.local_port}.")


def open_ssh_login(cfg: Config, s: Service) -> None:
    open_in_terminal(login_command(cfg, s, find_ssh() or "ssh"), title=f"SSH {s.remote_host}")


def rdp_file(cfg: Config, s: Service) -> Path:
    path = config_dir() / "rdp" / f"{s.id}.rdp"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\r\n".join([
        f"full address:s:{LOCALHOST}:{s.local_port}",
        f"username:s:{cfg.user}",
        "prompt for credentials:i:1",
        "screen mode id:i:1",
        "dynamic resolution:i:1",
        "",
    ]), encoding="utf-8")
    return path


def open_rdp(cfg: Config, s: Service) -> None:
    path = rdp_file(cfg, s)
    if WINDOWS or MACOS:  # mstsc / "Windows App" handle .rdp files
        open_path(str(path))
        return
    for exe in ("xfreerdp3", "xfreerdp", "wlfreerdp"):
        if shutil.which(exe):
            detached_popen([exe, f"/v:{LOCALHOST}:{s.local_port}", f"/u:{cfg.user}", "/dynamic-resolution", "/cert:tofu"])
            return
    if shutil.which("remmina"):
        detached_popen(["remmina", "-c", str(path)])
        return
    raise FileNotFoundError("No RDP client found. Install Remmina or FreeRDP, "
                            f"then connect to {LOCALHOST}:{s.local_port}.")


# ---------------------------------------------------------- ssh config ------
def ssh_config_path() -> Path:
    return Path.home() / ".ssh" / "config"


def ssh_config_block(cfg: Config) -> str:
    lines = [BEGIN,
             "# Log in to the gateway itself (password + OTP):",
             "Host psi-gateway",
             f"  HostName {cfg.gateway}",
             f"  Port {cfg.gateway_port}",
             f"  User {cfg.user}",
             "  ServerAliveInterval 30",
             "",
             "# Internal hosts; these work while the PSI tunnel is running:"]
    for s in cfg.services:
        if s.type != "ssh":
            continue
        lines += [f"Host psi-{s.id.removeprefix('ssh-')}",
                  f"  HostName {LOCALHOST}",
                  f"  Port {s.local_port}",
                  f"  HostKeyAlias {s.remote_host}",
                  f"  User {cfg.user}",
                  "  ServerAliveInterval 30",
                  ""]
    lines.append(END)
    return "\n".join(lines) + "\n"


def write_ssh_config(cfg: Config, remove: bool = False) -> Path:
    path = ssh_config_path()
    old = path.read_text(encoding="utf-8") if path.exists() else ""
    start, end = old.find(BEGIN), old.find(END)
    if start != -1 and end != -1:
        old = old[:start] + old[end + len(END):].lstrip("\n")
    new = old if remove else ssh_config_block(cfg) + ("\n" + old if old.strip() else "")
    # Our block goes first: ssh uses the first value it finds for each option.
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.exists():
        shutil.copy2(path, path.with_name("config.hoppla.bak"))
    path.write_text(new, encoding="utf-8")
    if not WINDOWS:
        path.chmod(0o600)
    return path


def ssh_aliases(cfg: Config) -> list[str]:
    return ["psi-gateway"] + [f"psi-{s.id.removeprefix('ssh-')}" for s in cfg.services if s.type == "ssh"]


def run_ssh_here(cfg: Config, s: Service) -> int:
    return subprocess.call(login_command(cfg, s, find_ssh() or "ssh"))
