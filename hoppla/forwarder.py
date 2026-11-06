"""A local port that leads to localhost:PORT on a host, through that host's SOCKS channel (ssh -D).

Used when a command prints a link to a port nobody configured (e.g. Jupyter took 8889): the port is
reachable on this computer without another login to the host.
"""

from __future__ import annotations

import socket
import threading

from .tunnel import LOCALHOST, port_free


class Forwarder:
    def __init__(self, socks_port: int, target_port: int, target_host: str = "localhost"):
        self.socks_port, self.target_host, self.target_port = socks_port, target_host, target_port
        self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server.bind((LOCALHOST, target_port if port_free(target_port) else 0))  # same number if free
        self.server.listen(20)
        self.port = self.server.getsockname()[1]
        threading.Thread(target=self._accept, daemon=True).start()

    def close(self) -> None:
        try:
            self.server.close()
        except OSError:
            pass

    def _accept(self) -> None:
        while True:
            try:
                client, _ = self.server.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(client,), daemon=True).start()

    def _serve(self, client: socket.socket) -> None:
        try:
            remote = socks5_connect(self.socks_port, self.target_host, self.target_port)
        except OSError:
            client.close()
            return
        relay(client, remote)


def socks5_connect(socks_port: int, host: str, port: int, timeout: float = 15) -> socket.socket:
    """Connect to host:port through the SOCKS5 proxy on 127.0.0.1:socks_port (name resolved there)."""
    s = socket.create_connection((LOCALHOST, socks_port), timeout=timeout)
    s.sendall(b"\x05\x01\x00")
    if s.recv(2) != b"\x05\x00":
        s.close()
        raise OSError("SOCKS proxy refused")
    name = host.encode()
    s.sendall(b"\x05\x01\x00\x03" + bytes([len(name)]) + name + port.to_bytes(2, "big"))
    reply = s.recv(10)
    if len(reply) < 2 or reply[1] != 0:
        s.close()
        raise OSError(f"cannot reach {host}:{port} on the host")
    s.settimeout(None)
    return s


def relay(a: socket.socket, b: socket.socket) -> None:
    """Copy both ways; when one side has finished sending, pass that on (half-close) and keep the other."""
    done = []

    def pump(src: socket.socket, dst: socket.socket) -> None:
        try:
            while True:
                data = src.recv(65536)
                if not data:
                    break
                dst.sendall(data)
        except OSError:
            pass
        try:
            dst.shutdown(socket.SHUT_WR)
        except OSError:
            pass
        done.append(1)
        if len(done) == 2:
            for x in (a, b):
                try:
                    x.close()
                except OSError:
                    pass

    threading.Thread(target=pump, args=(b, a), daemon=True).start()
    pump(a, b)
