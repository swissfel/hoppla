"""Configuration model, presets and persistence."""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

APP_NAME = "hoppla"
APP_TITLE = "Hoppla"
DEFAULT_GATEWAY = "hopx.psi.ch"
CONFIG_VERSION = 3

SERVICE_TYPES = {
    "socks": "SOCKS proxy (for browsers)",
    "forward": "Port forward (RDP, web, ...)",
    "ssh": "SSH login to internal host",
}
LOGIN_MODES = {
    "auto": "Automatic",
    "app": "Dialog window",
    "terminal": "Terminal window",
}
OPEN_ACTIONS = {
    "none": "nothing",
    "url": "open URL in default browser",
    "rdp": "remote desktop client",
}

WINDOWS = sys.platform.startswith("win")
MACOS = sys.platform == "darwin"


@dataclass
class Service:
    id: str
    name: str
    type: str  # socks | forward | ssh
    local_port: int
    remote_host: str = ""
    remote_port: int = 0
    via: str = "gateway"  # "gateway" (directly from hopx) or the id of an ssh service to hop through
    open: str = "none"  # forward only: none | url | rdp
    url: str = ""  # forward/url: template, "{port}" is replaced by local_port
    enabled: bool = False
    x11: bool = False  # ssh only: graphical programs (X11 forwarding) in the login

    def target(self) -> str:
        via = "" if self.via == "gateway" else f" via {self.via}"
        if self.type == "socks":
            return "any PSI address" + via
        port = self.remote_port or (22 if self.type == "ssh" else 0)
        return f"{self.remote_host}:{port}{via}"

    @property
    def ssh_port(self) -> int:
        return self.remote_port or 22

    def open_url(self) -> str:
        return self.url.replace("{port}", str(self.local_port))

    @classmethod
    def from_dict(cls, d: dict) -> "Service":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})


def presets() -> list[Service]:
    """Services offered out of the box. Adjust these for your facility.

    External users may only reach clusters such as Ra by SSH through hopx, so
    proxies and forwards run through Ra. "gateway" services need PSI staff rights.
    """
    return [
        Service("ssh-ra", "SSH to Ra cluster", "ssh", 2201, "ra.psi.ch", 22),
        Service("socks-ra", "Web proxy via Ra", "socks", 8989, via="ssh-ra", enabled=True),
        Service("jupyter-ra", "Jupyter on Ra", "forward", 9789, "jupytera.psi.ch", 443, via="ssh-ra",
                open="url", url="https://localhost:{port}/"),
        Service("socks", "Web proxy on hopx (PSI staff)", "socks", 1080),
        Service("rdp-winterm", "Windows terminal server, RDP (PSI staff)", "forward", 9022,
                "winterm6.psi.ch", 3389, open="rdp"),
    ]


@dataclass
class Config:
    user: str = ""
    gateway: str = DEFAULT_GATEWAY
    gateway_port: int = 22
    browser: str = ""
    browser_proxy: str = ""  # id of the SOCKS service "Open browser" uses; "" = automatic
    login_mode: str = "auto"  # auto | app (password dialogs) | terminal
    pac_only_psi: bool = False
    system_proxy: bool = False  # macOS: PSI addresses of all apps through the tunnel (sysproxy.py)
    psi_domains: list[str] = field(default_factory=lambda: ["psi.ch"])
    psi_networks: list[str] = field(default_factory=lambda: ["129.129."])
    start_url: str = "https://www.psi.ch/"
    services: list[Service] = field(default_factory=presets)
    version: int = CONFIG_VERSION

    # ---- lookup -------------------------------------------------------
    def service(self, sid: str) -> Service:
        for s in self.services:
            if s.id == sid:
                return s
        raise KeyError(f"unknown service '{sid}'")

    def selected(self, extra_ids=()) -> list[Service]:
        """Enabled services plus `extra_ids`, plus every ssh host they hop through."""
        ids = {s.id for s in self.services if s.enabled} | set(extra_ids)
        by_id = {s.id: s for s in self.services}
        todo = list(ids)
        while todo:
            s = by_id.get(todo.pop())
            if s and s.via != "gateway" and s.via not in ids:
                ids.add(s.via)
                todo.append(s.via)
        return [s for s in self.services if s.id in ids]

    def chain(self, s: Service) -> list[Service]:
        """The ssh hosts between hopx and `s`, nearest to hopx first; raises ValueError on a loop."""
        hops, seen = [], {s.id}
        while s.via != "gateway":
            s = self.service(s.via)
            if s.id in seen:
                raise ValueError("loop")
            seen.add(s.id)
            hops.insert(0, s)
        return hops

    def new_id(self, name: str) -> str:
        base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "service"
        sid, n = base, 2
        existing = {s.id for s in self.services}
        while sid in existing:
            sid, n = f"{base}-{n}", n + 1
        return sid

    # ---- validation ---------------------------------------------------
    def problems(self, services: list[Service] | None = None) -> list[str]:
        out = []
        if not self.user.strip():
            out.append("PSI user name is not set.")
        if not self.gateway.strip():
            out.append("Gateway host is not set.")
        services = self.selected() if services is None else services
        seen: dict[int, str] = {}
        for s in services:
            if not 1024 <= int(s.local_port) <= 65535:
                out.append(f"{s.name}: local port must be between 1024 and 65535.")
            if s.local_port in seen:
                out.append(f"{s.name}: local port {s.local_port} also used by {seen[s.local_port]}.")
            seen[s.local_port] = s.name
            if s.type in ("forward", "ssh") and not s.remote_host.strip():
                out.append(f"{s.name}: remote host is missing.")
            if s.type == "forward" and not s.remote_port:
                out.append(f"{s.name}: remote port is missing.")
            if s.via != "gateway":
                try:
                    if self.service(s.via).type != "ssh":
                        out.append(f"{s.name}: '{s.via}' is not an SSH service.")
                    else:
                        self.chain(s)
                except KeyError:
                    out.append(f"{s.name}: unknown SSH service '{s.via}'.")
                except ValueError:
                    out.append(f"{s.name}: its 'runs through' hosts form a loop.")
        return out

    # ---- persistence --------------------------------------------------
    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Config":
        known = {f.name for f in fields(cls)} - {"services"}
        cfg = cls(**{k: v for k, v in d.items() if k in known})
        if "services" in d and d.get("version", 1) >= 2:
            cfg.services = [Service.from_dict(s) for s in d["services"]]
            for s in cfg.services:  # version 3: winterm3 was replaced by winterm6
                if s.remote_host == "winterm3.psi.ch":
                    s.remote_host = "winterm6.psi.ch"
        cfg.version = CONFIG_VERSION  # version 1 lists assumed forwards directly on hopx: use new presets
        return cfg

    def save(self, path: Path | None = None) -> None:
        path = path or config_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")
        os.replace(tmp, path)


def load(path: Path | None = None) -> Config:
    path = path or config_file()
    if path.exists():
        return Config.from_dict(json.loads(path.read_text(encoding="utf-8")))
    return Config(user=os.environ.get("PSI_USER", ""))


def config_dir() -> Path:
    override = os.environ.get("HOPPLA_HOME")
    if override:
        return Path(override)
    if WINDOWS:
        return Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / APP_NAME
    if MACOS:
        return Path.home() / "Library" / "Application Support" / APP_NAME
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / APP_NAME


def config_file() -> Path:
    return config_dir() / "config.json"


def state_file() -> Path:
    return config_dir() / "state.json"
