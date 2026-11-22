"""Bridge for the Hoppla web page: lets it see and reuse the session Hoppla already holds.

Start it next to Hoppla:

    hoppla serve                      # allows the page on https://swissfel.github.io
    hoppla serve --origin https://OTHER.example

It listens on 127.0.0.1 only, prints a pairing code, and offers

    GET  /hello    "is a bridge here?" (no code needed)
    GET  /status   JSON about the open Hoppla session (needs the pairing code)
    WS   /ws       raw TCP to a local port that Hoppla itself forwards (needs the code)

Safety rules, because any web page can try to talk to localhost:
  * the Origin header must be one of the allowed origins, else 403
  * every call except /hello needs the pairing code
  * /ws only reaches ports of Hoppla services that are running AND listening
    (never an arbitrary local port) and never opens a second gateway login
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import secrets
import socket
import struct
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from .config import load
from .tunnel import read_state

VERSION = "0.1"
DEFAULT_ORIGIN = "https://swissfel.github.io"
PAGE_URL = "https://swissfel.github.io/hoppla/"
GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
SESSION_LIMIT_S = 12 * 3600  # the gateway ends sessions after 12 hours
LOCAL = "127.0.0.1"


# ------------------------------------------------------------ Hoppla state --
def listening(port: int, timeout: float = 0.25) -> bool:
    try:
        with socket.create_connection((LOCAL, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def session_info() -> dict:
    """What the page may know about the Hoppla session. Never contains passwords."""
    try:
        cfg = load()
    except (OSError, ValueError, TypeError):
        return {"connected": False, "user": "", "gateway": "", "services": []}
    state = read_state()  # {} unless the session runner is alive
    if not state:
        return {"connected": False, "user": cfg.user, "gateway": cfg.gateway, "services": []}
    by_id = {s.id: s for s in cfg.services}
    services = []
    for sid in state.get("services", []):
        s = by_id.get(sid)
        if s is None:
            services.append({"id": sid, "name": sid, "type": "", "port": 0, "target": "", "target_port": 0,
                             "via": "", "url": "", "listening": False})
            continue
        port = int(s.local_port or 0)
        services.append({
            "id": s.id, "name": s.name, "type": s.type,
            "port": port, "target": s.remote_host, "target_port": s.remote_port,
            "via": s.via, "url": s.open_url() if s.url else "",
            "listening": bool(port) and listening(port),
        })
    started = float(state.get("started", time.time()))
    age = max(0, int(time.time() - started))
    return {"connected": True, "user": cfg.user, "gateway": cfg.gateway,
            "started": started, "age_s": age, "expires_in_s": max(0, SESSION_LIMIT_S - age),
            "services": services}


def allowed_ports() -> set[int]:
    """Ports a page may reach: running, listening ssh/forward services of the open session."""
    info = session_info()
    return {s["port"] for s in info["services"] if s["listening"] and s["type"] in ("ssh", "forward")}


# ------------------------------------------------------------------ server --
class Bridge(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, addr, origins, token):
        super().__init__(addr, Handler)
        self.origins, self.token = set(origins), token

    def handle_error(self, request, client_address):
        if isinstance(sys.exc_info()[1], (ConnectionError, TimeoutError)):
            return  # a browser tab closing mid-request is normal
        super().handle_error(request, client_address)


class Handler(BaseHTTPRequestHandler):
    server: Bridge
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # quiet; no URLs with codes in logs
        pass

    # -- helpers
    def origin_ok(self) -> bool:
        return self.headers.get("Origin", "") in self.server.origins

    def cors(self):
        o = self.headers.get("Origin", "")
        if o in self.server.origins:
            self.send_header("Access-Control-Allow-Origin", o)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Headers", "X-Hoppla-Code, Content-Type")
            self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
            self.send_header("Access-Control-Allow-Private-Network", "true")
            self.send_header("Access-Control-Max-Age", "60")

    def reply(self, code: int, body: dict | None = None):
        data = json.dumps(body or {}).encode()
        self.send_response(code)
        self.cors()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def code_ok(self, supplied: str) -> bool:
        return secrets.compare_digest(supplied.strip().lower().encode(), self.server.token.encode())

    # -- routes
    def do_OPTIONS(self):
        if not self.origin_ok():
            return self.reply(403, {"error": "origin not allowed"})
        self.send_response(204)
        self.cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        if not self.origin_ok():
            return self.reply(403, {"error": "origin not allowed"})
        path = urlparse(self.path).path
        if path == "/hello":
            return self.reply(200, {"bridge": VERSION})
        if path == "/status":
            if not self.code_ok(self.headers.get("X-Hoppla-Code", "")):
                return self.reply(401, {"bridge": VERSION, "error": "pairing code needed"})
            return self.reply(200, {"bridge": VERSION, **session_info()})
        if path == "/ws" and self.headers.get("Upgrade", "").lower() == "websocket":
            return self.websocket()
        self.reply(404, {"error": "not found"})

    # -- websocket to a Hoppla-owned local port
    def websocket(self):
        key = self.headers.get("Sec-WebSocket-Key", "")
        if not key:
            return self.reply(400, {"error": "bad websocket request"})
        accept = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
        self.send_response(101, "Switching Protocols")
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.end_headers()
        self.wfile.flush()
        self.close_connection = True
        sock = self.connection
        sock.settimeout(15)
        try:
            op, payload = ws_read(sock)
            parts = payload.decode("utf-8", "replace").split()
            # first message: "auth CODE PORT"
            if op != 1 or len(parts) != 3 or parts[0] != "auth" or not self.code_ok(parts[1]):
                return ws_close(sock, 4401, "pairing code needed")
            port = int(parts[2]) if parts[2].isdigit() else 0
            if port not in allowed_ports():
                return ws_close(sock, 4403, "that port is not a running Hoppla service")
            remote = socket.create_connection((LOCAL, port), timeout=10)
        except (OSError, ValueError, ConnectionError):
            return
        sock.settimeout(None)
        remote.settimeout(None)
        ws_send(sock, 1, b"ok")
        pump(sock, remote)


# ----------------------------------------------------------- ws framing -----
def recv_exact(sock, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("closed")
        buf += chunk
    return buf


def ws_read(sock) -> tuple[int, bytes]:
    """One whole message from a client (clients must mask). Returns (opcode, payload)."""
    data, opcode = b"", 0
    while True:
        b1, b2 = recv_exact(sock, 2)
        fin, op, masked, ln = b1 & 0x80, b1 & 0x0F, b2 & 0x80, b2 & 0x7F
        if ln == 126:
            ln = struct.unpack(">H", recv_exact(sock, 2))[0]
        elif ln == 127:
            ln = struct.unpack(">Q", recv_exact(sock, 8))[0]
        if ln > 4 * 1024 * 1024:
            raise ConnectionError("frame too large")
        mask = recv_exact(sock, 4) if masked else b""
        payload = recv_exact(sock, ln) if ln else b""
        if masked:
            payload = bytes(c ^ mask[i % 4] for i, c in enumerate(payload))
        if op == 8:
            return 8, payload
        if op == 9:
            ws_send(sock, 10, payload)
            continue
        if op == 10:
            continue
        if op:
            opcode = op
        data += payload
        if fin:
            return opcode, data


_send_lock = threading.Lock()


def ws_send(sock, op: int, payload: bytes):
    n = len(payload)
    if n < 126:
        head = bytes([0x80 | op, n])
    elif n < 65536:
        head = bytes([0x80 | op, 126]) + struct.pack(">H", n)
    else:
        head = bytes([0x80 | op, 127]) + struct.pack(">Q", n)
    with _send_lock:
        sock.sendall(head + payload)


def ws_close(sock, code: int, reason: str):
    try:
        ws_send(sock, 8, struct.pack(">H", code) + reason.encode())
    except OSError:
        pass


def pump(ws, remote):
    """Binary websocket frames <-> raw TCP, until either side ends."""
    def to_ws():
        try:
            while True:
                data = remote.recv(65536)
                if not data:
                    break
                ws_send(ws, 2, data)
        except OSError:
            pass
        ws_close(ws, 1000, "remote closed")
        try:
            ws.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    threading.Thread(target=to_ws, daemon=True).start()
    try:
        while True:
            op, payload = ws_read(ws)
            if op == 8:
                break
            remote.sendall(payload)
    except (OSError, ConnectionError):
        pass
    try:
        remote.close()
    except OSError:
        pass


# --------------------------------------------------------------------- cli --
def make_code() -> str:
    alphabet = "abcdefghjkmnpqrstuvwxyz23456789"
    pick = lambda: "".join(secrets.choice(alphabet) for _ in range(4))  # noqa: E731
    return pick() + "-" + pick()


def add_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument("--origin", action="append", default=[], metavar="URL",
                   help=f"web page origin allowed to use the bridge (repeatable; default {DEFAULT_ORIGIN})")
    p.add_argument("--port", type=int, default=8760)
    p.add_argument("--code", help="pairing code (default: a new random one)")


def serve(origins: list[str], port: int = 8760, code: str | None = None) -> int:
    origins = [o.rstrip("/") for o in origins] or [DEFAULT_ORIGIN]
    token = (code or make_code()).lower()
    try:
        srv = Bridge((LOCAL, port), origins, token)
    except OSError as e:
        print(f"Error: cannot listen on {LOCAL}:{port} ({e}). Is a bridge already running?", file=sys.stderr)
        return 1
    print(f"Hoppla bridge {VERSION} on http://{LOCAL}:{srv.server_address[1]}")
    print(f"Allowed web page origins: {', '.join(origins)}")
    if origins == [DEFAULT_ORIGIN]:
        print(f"Web page: {PAGE_URL}")
    print(f"Pairing code: {token}   (type it into the web page once)")
    print("Stop with Ctrl+C.")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="hoppla serve", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    add_arguments(ap)
    a = ap.parse_args(argv)
    return serve(a.origin, a.port, a.code)


if __name__ == "__main__":
    sys.exit(main())
