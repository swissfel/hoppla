import base64
import http.client
import json
import os
import socket
import struct
import tempfile
import threading
import time
import unittest
from pathlib import Path

from hoppla import bridge as hb

ORIGIN = "https://example.github.io"
CODE = "test-code"


def fake_ssh_server():
    """A TCP server that says what an SSH server says first, then echoes."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(5)

    def run():
        while True:
            try:
                c, _ = srv.accept()
            except OSError:
                return
            c.sendall(b"SSH-2.0-FakeRa\r\n")

            def echo(conn=c):
                try:
                    while True:
                        d = conn.recv(4096)
                        if not d:
                            break
                        conn.sendall(d)
                except OSError:
                    pass
                conn.close()
            threading.Thread(target=echo, daemon=True).start()

    threading.Thread(target=run, daemon=True).start()
    return srv


def ws_connect(port, origin=ORIGIN):
    s = socket.create_connection(("127.0.0.1", port), timeout=5)
    key = base64.b64encode(os.urandom(16)).decode()
    s.sendall((f"GET /ws HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
               f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\nOrigin: {origin}\r\n\r\n").encode())
    buf = b""
    while b"\r\n\r\n" not in buf:
        buf += s.recv(1)
    return s, buf.decode()


def ws_send_text(s, text):
    data, mask = text.encode(), os.urandom(4)
    s.sendall(bytes([0x81, 0x80 | len(data)]) + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))


def ws_recv(s):
    b1, b2 = s.recv(2)
    ln = b2 & 0x7F
    if ln == 126:
        ln = struct.unpack(">H", s.recv(2))[0]
    payload = b""
    while len(payload) < ln:
        payload += s.recv(ln - len(payload))
    return b1 & 0x0F, payload


class BridgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.old_home = os.environ.get("HOPPLA_HOME")
        os.environ["HOPPLA_HOME"] = cls.tmp.name
        cls.ra = fake_ssh_server()
        cls.ra_port = cls.ra.getsockname()[1]
        cls.write_state(connected=True)
        cls.srv = hb.Bridge(("127.0.0.1", 0), [ORIGIN], CODE)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.ra.close()
        cls.tmp.cleanup()
        if cls.old_home is None:
            os.environ.pop("HOPPLA_HOME", None)
        else:
            os.environ["HOPPLA_HOME"] = cls.old_home

    @classmethod
    def write_state(cls, connected):
        d = Path(cls.tmp.name)
        (d / "config.json").write_text(json.dumps({
            "version": 3, "user": "tester", "gateway": "hopx.psi.ch",
            "services": [
                {"id": "ssh-ra", "name": "SSH to Ra cluster", "type": "ssh", "local_port": cls.ra_port,
                 "remote_host": "ra.psi.ch", "remote_port": 22, "via": "gateway"},
                {"id": "jupyter-ra", "name": "Jupyter on Ra", "type": "forward", "local_port": 9789,
                 "remote_host": "jupytera.psi.ch", "remote_port": 443, "via": "ssh-ra",
                 "url": "https://localhost:{port}/"},
            ]}))
        if connected:
            (d / "state.json").write_text(json.dumps({
                "runner_pid": os.getpid(), "services": ["ssh-ra", "jupyter-ra"], "started": time.time() - 60}))
        else:
            (d / "state.json").unlink(missing_ok=True)

    def get(self, path, origin=ORIGIN, code=None, method="GET"):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        h = {}
        if origin:
            h["Origin"] = origin
        if code:
            h["X-Hoppla-Code"] = code
        c.request(method, path, headers=h)
        r = c.getresponse()
        body = r.read()
        c.close()
        return r.status, dict(r.getheaders()), (json.loads(body) if body else {})

    def test_wrong_origin_is_refused(self):
        self.assertEqual(self.get("/hello", origin="https://evil.example")[0], 403)
        self.assertEqual(self.get("/hello", origin=None)[0], 403)

    def test_hello_needs_no_code(self):
        st, h, body = self.get("/hello")
        self.assertEqual(st, 200)
        self.assertEqual(h["Access-Control-Allow-Origin"], ORIGIN)
        self.assertIn("bridge", body)

    def test_preflight_allows_private_network(self):
        st, h, _ = self.get("/status", method="OPTIONS")
        self.assertEqual(st, 204)
        self.assertEqual(h["Access-Control-Allow-Private-Network"], "true")
        self.assertIn("X-Hoppla-Code", h["Access-Control-Allow-Headers"])

    def test_status_needs_code(self):
        self.assertEqual(self.get("/status")[0], 401)
        self.assertEqual(self.get("/status", code="wrong")[0], 401)

    def test_status_when_connected(self):
        self.write_state(connected=True)
        st, _, body = self.get("/status", code=CODE.upper())
        self.assertEqual(st, 200)
        self.assertTrue(body["connected"])
        self.assertEqual(body["user"], "tester")
        ids = {s["id"]: s for s in body["services"]}
        self.assertTrue(ids["ssh-ra"]["listening"])
        self.assertFalse(ids["jupyter-ra"]["listening"])  # nothing listens on 9789 in the test
        self.assertEqual(ids["jupyter-ra"]["url"], "https://localhost:9789/")
        self.assertTrue(0 < body["expires_in_s"] <= 12 * 3600)

    def test_status_when_not_connected(self):
        self.write_state(connected=False)
        try:
            st, _, body = self.get("/status", code=CODE)
            self.assertEqual(st, 200)
            self.assertFalse(body["connected"])
            self.assertEqual(body["services"], [])
        finally:
            self.write_state(connected=True)

    def test_websocket_reaches_banner_and_echoes(self):
        self.write_state(connected=True)
        s, head = ws_connect(self.port)
        self.assertIn("101", head.split("\r\n")[0])
        ws_send_text(s, f"auth {CODE} {self.ra_port}")
        op, payload = ws_recv(s)
        self.assertEqual((op, payload), (1, b"ok"))
        op, payload = ws_recv(s)
        self.assertEqual(op, 2)
        self.assertTrue(payload.startswith(b"SSH-2.0-"))
        s.close()

    def test_websocket_refuses_wrong_code(self):
        s, _ = ws_connect(self.port)
        ws_send_text(s, f"auth nope {self.ra_port}")
        op, payload = ws_recv(s)
        self.assertEqual(op, 8)
        self.assertEqual(struct.unpack(">H", payload[:2])[0], 4401)
        s.close()

    def test_websocket_refuses_unlisted_port(self):
        other = socket.socket()
        other.bind(("127.0.0.1", 0))
        other.listen(1)
        try:
            s, _ = ws_connect(self.port)
            ws_send_text(s, f"auth {CODE} {other.getsockname()[1]}")
            op, payload = ws_recv(s)
            self.assertEqual(op, 8)
            self.assertEqual(struct.unpack(">H", payload[:2])[0], 4403)
            s.close()
        finally:
            other.close()

    def test_cli_serve_options(self):
        from hoppla.cli import build_parser
        a = build_parser().parse_args(["serve", "--origin", "https://a.example", "--port", "9000"])
        self.assertEqual((a.cmd, a.origin, a.port, a.code), ("serve", ["https://a.example"], 9000, None))
        self.assertEqual(build_parser().parse_args(["serve"]).origin, [])

    def test_websocket_wrong_origin(self):
        s, head = ws_connect(self.port, origin="https://evil.example")
        self.assertIn("403", head.split("\r\n")[0])
        s.close()


if __name__ == "__main__":
    unittest.main()
