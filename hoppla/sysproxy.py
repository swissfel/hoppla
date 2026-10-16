"""macOS only: route PSI addresses of all apps (Safari, Mail, ...) through the tunnel.

While connected, every enabled network service gets an automatic proxy configuration
(PAC) that sends only PSI addresses to Hoppla's SOCKS proxy; everything else stays
direct. The PAC file is served over http://127.0.0.1 by the session runner, because
macOS no longer accepts file:// PAC URLs -- and if Hoppla is gone, the URL fails and
macOS connects directly. The previous settings are saved to a file and restored on
disconnect, or at the next start after a crash.
"""

from __future__ import annotations

import http.server
import json
import shlex
import shutil
import socketserver
import subprocess
import threading
from pathlib import Path

from .config import Config, config_dir

EXPLANATION = """Use the Mac's system proxy for PSI addresses?

What it does: while Hoppla is connected, macOS sends only PSI addresses (*.psi.ch, short host names, 129.129.*) through the tunnel. Everything else goes directly, as before. Hoppla removes the setting again when you disconnect.

Pros
 • Safari, Mail and other Mac apps reach PSI pages, not only the separate Hoppla browser window.
 • Your usual browser with its bookmarks and saved passwords is used.

Cons
 • Hoppla changes your network settings while connected; macOS may ask for your administrator password.
 • If Hoppla or the Mac crashes while connected, the setting stays until Hoppla starts again and cleans it up. Until then PSI addresses do not load; other sites are not affected.
 • A proxy configuration you already use (e.g. from your employer) is replaced while connected and restored afterwards.
 • Experimental: not yet tested on many Macs.

Needs a "Web proxy" service ticked (it provides the tunnel for the browsers).

Turn it on?"""

NETWORKSETUP = "networksetup"


def state_file() -> Path:
    return config_dir() / "system-proxy.json"


def available() -> bool:
    return shutil.which(NETWORKSETUP) is not None


def _run(*args: str) -> str:
    out = subprocess.run([NETWORKSETUP, *args], capture_output=True, text=True, timeout=30)
    return out.stdout + out.stderr


def services() -> list[str]:
    """Enabled network services (Wi-Fi, Ethernet, ...); disabled ones are marked with '*'."""
    lines = _run("-listallnetworkservices").splitlines()
    return [ln.strip() for ln in lines[1:] if ln.strip() and not ln.startswith("*")]


def _current(service: str) -> dict:
    text = _run("-getautoproxyurl", service)
    url, enabled = "", False
    for ln in text.splitlines():
        if ln.startswith("URL:"):
            url = ln.split(":", 1)[1].strip()
            url = "" if url == "(null)" else url
        if ln.startswith("Enabled:"):
            enabled = ln.split(":", 1)[1].strip().lower() == "yes"
    return {"url": url, "enabled": enabled}


def _apply(commands: list[list[str]]) -> None:
    """Run networksetup commands; if macOS wants an administrator, ask once for all of them."""
    failed = []
    for cmd in commands:
        out = _run(*cmd)
        if "requires admin" in out.lower() or "error" in out.lower():
            failed.append(cmd)
    if not failed:
        return
    script = " && ".join(shlex.join([NETWORKSETUP, *c]) for c in failed)
    escaped = script.replace("\\", "\\\\").replace('"', '\\"')
    out = subprocess.run(["osascript", "-e", f'do shell script "{escaped}" with administrator privileges'],
                         capture_output=True, text=True, timeout=300)
    if out.returncode:
        raise PermissionError("Changing the network settings was not allowed: " + (out.stderr.strip() or "cancelled"))


def enable(pac_url: str) -> None:
    saved = {s: _current(s) for s in services()}
    if not state_file().exists():  # keep the original settings if a crashed session left ours behind
        state_file().write_text(json.dumps(saved), encoding="utf-8")
    cmds = []
    for s in saved:
        cmds += [["-setautoproxyurl", s, pac_url], ["-setautoproxystate", s, "on"]]
    _apply(cmds)


def restore() -> bool:
    """Put back the settings saved by enable(); returns True if there was something to restore."""
    try:
        saved = json.loads(state_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    cmds = []
    for s, old in saved.items():
        # a service without a PAC before keeps our (now switched off, harmless) URL in the field:
        # networksetup cannot reliably set an empty URL
        if old.get("url"):
            cmds.append(["-setautoproxyurl", s, old["url"]])
        cmds.append(["-setautoproxystate", s, "on" if old.get("enabled") else "off"])
    _apply(cmds)
    state_file().unlink()
    return True


def cleanup_leftovers() -> bool:
    """After a crash: restore the settings a session could not restore itself."""
    if not state_file().exists() or not available():
        return False
    try:
        return restore()
    except Exception:  # noqa: BLE001 - try again next time
        return False


class PacServer:
    """Serves the PAC file on http://127.0.0.1:<random port>/hoppla.pac."""

    def __init__(self, pac_text: str):
        body = pac_text.encode()

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "application/x-ns-proxy-autoconfig")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/hoppla.pac"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def socks_port(cfg: Config, service_ids) -> int | None:
    """Port of the first SOCKS proxy in the running session."""
    for s in cfg.services:
        if s.type == "socks" and s.id in service_ids:
            return s.local_port
    return None
