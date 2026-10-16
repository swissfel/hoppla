"""Command line interface. Without arguments the graphical window opens."""

from __future__ import annotations

import argparse
import os
import sys

from . import __version__, actions, browsers, shortcuts, tunnel
from .config import APP_TITLE, Config, load
from .platforms import find_ssh, ssh_install_hint


def _pick_socks(cfg: Config, sid: str | None):
    if sid:
        return cfg.service(sid)
    socks = [s for s in cfg.services if s.type == "socks"]
    if not socks:
        raise tunnel.TunnelError("No SOCKS proxy service is configured.")
    running = set(tunnel.running_services())
    for group in ([s for s in socks if s.id in running], [s for s in socks if s.enabled], socks):
        if group:
            return group[0]


def cmd_browser(cfg: Config, a) -> int:
    name = a.name or cfg.browser or next(iter(browsers.detect()), None)
    if not name:
        raise tunnel.TunnelError("No supported browser (Firefox, Chrome, Edge, Chromium, Brave) found.")
    socks = _pick_socks(cfg, a.service)
    tunnel.ensure_service(cfg, socks.id, progress=None)
    pac = True if a.psi_only else False if a.all_traffic else None
    browsers.launch(cfg, name, socks.local_port, a.url, pac)
    return 0


def cmd_open(cfg: Config, a) -> int:
    s = cfg.service(a.service)
    tunnel.ensure_service(cfg, s.id)
    actions.open_service(cfg, s)
    return 0


def cmd_ssh(cfg: Config, a) -> int:
    s = cfg.service(a.service)
    if s.type != "ssh":
        raise tunnel.TunnelError(f"'{s.id}' is not an SSH service.")
    if sys.stdin and sys.stdin.isatty():
        if not tunnel.listening(s.local_port):
            tunnel.ensure_service(cfg, s.id)
        return actions.run_ssh_here(cfg, s)
    tunnel.ensure_service(cfg, s.id)
    actions.open_ssh_login(cfg, s)
    return 0


def cmd_start(cfg: Config, a) -> int:
    if a.here:
        from .platforms import ensure_console
        ensure_console("Hoppla")
        return tunnel.run_session(cfg, a.with_)
    mode = tunnel.start(cfg, a.with_)
    print("Log in in the window that just opened." if mode == "app" else "A terminal window opened for the login.")
    return 0


def cmd_status(cfg: Config, a) -> int:
    running = set(tunnel.running_services())
    print("Tunnel:", "running" if running else "stopped")
    for s in cfg.services:
        mark = "x" if s.enabled else " "
        up = "up" if s.id in running and tunnel.listening(s.local_port) else ("..." if s.id in running else "")
        print(f" [{mark}] {s.id:16} {s.local_port:6}  {s.type:8} {s.target():34} {up}")
    return 0


def cmd_shortcut(cfg: Config, a) -> int:
    what = a.what
    if what == "gui":
        label, args, kind = APP_TITLE, [], "gui"
    elif what == "tunnel":
        label, args, kind = "Hoppla - connect", ["start"], "tunnel"
    elif what == "browser":
        name = a.target or cfg.browser or next(iter(browsers.detect()), "firefox")
        label, args, kind = f"PSI {browsers.NAMES.get(name, name)}", ["browser", name], "browser"
    else:
        s = cfg.service(a.target or "")
        label = f"PSI {s.name}"
        args, kind = (["ssh", s.id], "ssh") if s.type == "ssh" else (["open", s.id], s.open)
    for p in shortcuts.create(label, args, kind, desktop=not a.no_desktop):
        print("created", p)
    return 0


def cmd_ssh_config(cfg: Config, a) -> int:
    if a.print:
        print(actions.ssh_config_block(cfg), end="")
        return 0
    path = actions.write_ssh_config(cfg, remove=a.remove)
    print(("removed block from " if a.remove else "updated ") + str(path))
    return 0


def cmd_config(cfg: Config, a) -> int:
    if a.user:
        cfg.user = a.user
    if a.gateway:
        cfg.gateway = a.gateway
    for sid in a.enable or []:
        cfg.service(sid).enabled = True
    for sid in a.disable or []:
        cfg.service(sid).enabled = False
    if a.browser:
        cfg.browser = a.browser
    if a.login:
        cfg.login_mode = a.login
    if a.system_proxy:
        cfg.system_proxy = a.system_proxy == "on"
    cfg.save()
    from .config import config_file
    print("saved", config_file())
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="hoppla", description="SSH tunnels into PSI via the hopx gateway.")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd")

    sub.add_parser("gui", help="open the graphical window (default)")
    s = sub.add_parser("start", help="open a terminal window and connect")
    s.add_argument("--here", action="store_true", help="run in this terminal instead")
    s.add_argument("--with", dest="with_", action="append", default=[], metavar="ID",
                   help="also include this service")
    s = sub.add_parser("session")  # internal: the runner started by "start"
    s.add_argument("--with", dest="with_", action="append", default=[])
    s.add_argument("--background", action="store_true")
    sub.add_parser("stop", help="disconnect")
    sub.add_parser("install", help="copy this program to a permanent place and add a menu entry")
    sub.add_parser("status", help="show services and tunnel state")

    s = sub.add_parser("browser", help="start a browser that uses the tunnel (connects if needed)")
    s.add_argument("name", nargs="?", choices=list(browsers.NAMES))
    s.add_argument("--service", help="SOCKS service id")
    s.add_argument("--url")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--psi-only", action="store_true", help="only PSI addresses go through the tunnel")
    g.add_argument("--all-traffic", action="store_true", help="all browser traffic goes through the tunnel")

    s = sub.add_parser("open", help="open a forwarded service (RDP, web page, ...)")
    s.add_argument("service")
    s = sub.add_parser("ssh", help="log in to an internal host through the tunnel")
    s.add_argument("service")

    s = sub.add_parser("shortcut", help="create a desktop / menu shortcut")
    s.add_argument("what", choices=["gui", "tunnel", "browser", "service"])
    s.add_argument("target", nargs="?", help="browser name or service id")
    s.add_argument("--no-desktop", action="store_true", help="only add to the start menu / applications")

    s = sub.add_parser("ssh-config", help="add 'ssh psi-...' aliases to ~/.ssh/config")
    s.add_argument("--remove", action="store_true")
    s.add_argument("--print", action="store_true", help="only print the block")

    s = sub.add_parser("config", help="change settings without the GUI")
    s.add_argument("--user")
    s.add_argument("--gateway")
    s.add_argument("--browser", choices=list(browsers.NAMES))
    s.add_argument("--login", choices=["auto", "app", "terminal"], help="where password/OTP are asked")
    s.add_argument("--system-proxy", choices=["on", "off"],
                   help="macOS: PSI addresses of all apps (Safari, ...) through the tunnel while connected")
    s.add_argument("--enable", action="append", metavar="ID")
    s.add_argument("--disable", action="append", metavar="ID")
    return p


COMMANDS = {
    "start": cmd_start, "status": cmd_status, "browser": cmd_browser, "open": cmd_open,
    "ssh": cmd_ssh, "shortcut": cmd_shortcut, "ssh-config": cmd_ssh_config, "config": cmd_config,
}


def main(argv: list[str] | None = None) -> int:
    if os.environ.get(tunnel.ASKPASS_ENV) == "1":  # we were started by ssh as SSH_ASKPASS
        from .askpass import main as askpass_main
        return askpass_main(sys.argv[1:] if argv is None else argv)
    a = build_parser().parse_args(argv)
    if a.cmd in (None, "gui"):
        from .gui import run_gui
        return run_gui()
    if a.cmd == "session":
        return tunnel.session_main(a.with_, a.background)
    if a.cmd == "install":
        from .install import install
        made = install()
        print("\n".join(map(str, made)) if made else "Nothing to do (running from source or on macOS).")
        return 0
    if a.cmd == "stop":
        print("stopped" if tunnel.stop() else "not running")
        return 0
    cfg = load()
    if a.cmd in ("start", "browser", "open", "ssh") and not find_ssh():
        return _fail(ssh_install_hint())
    try:
        return COMMANDS[a.cmd](cfg, a)
    except (tunnel.TunnelError, KeyError, ValueError, FileNotFoundError, RuntimeError) as e:
        return _fail(str(e).strip("'\""))


def _fail(msg: str) -> int:
    """Report errors visibly, also when started from a desktop icon without a console."""
    if sys.stderr is not None:
        try:
            print("Error:", msg, file=sys.stderr)
        except (OSError, ValueError):
            pass
    if sys.stdin is None or not sys.stdin.isatty():
        try:
            import tkinter
            from tkinter import messagebox
            root = tkinter.Tk()
            root.withdraw()
            messagebox.showerror(APP_TITLE, msg)
            root.destroy()
        except Exception:
            pass
    return 1
