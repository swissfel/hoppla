"""Find browsers and start them with a private profile that uses the SOCKS tunnel."""

from __future__ import annotations

import base64
import os
import shutil
from pathlib import Path

from .config import MACOS, WINDOWS, Config, config_dir
from .platforms import detached_popen

NAMES = {
    "firefox": "Firefox",
    "chrome": "Google Chrome",
    "edge": "Microsoft Edge",
    "chromium": "Chromium",
    "brave": "Brave",
}


def _win_candidates(name: str) -> list[Path]:
    roots = [os.environ.get(k) for k in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA")]
    rel = {
        "firefox": [r"Mozilla Firefox\firefox.exe"],
        "chrome": [r"Google\Chrome\Application\chrome.exe"],
        "edge": [r"Microsoft\Edge\Application\msedge.exe"],
        "chromium": [r"Chromium\Application\chrome.exe"],
        "brave": [r"BraveSoftware\Brave-Browser\Application\brave.exe"],
    }[name]
    return [Path(r) / p for r in roots if r for p in rel]


def _mac_candidates(name: str) -> list[Path]:
    app = {
        "firefox": "Firefox.app/Contents/MacOS/firefox",
        "chrome": "Google Chrome.app/Contents/MacOS/Google Chrome",
        "edge": "Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        "chromium": "Chromium.app/Contents/MacOS/Chromium",
        "brave": "Brave Browser.app/Contents/MacOS/Brave Browser",
    }[name]
    return [Path("/Applications") / app, Path.home() / "Applications" / app]


LINUX_COMMANDS = {
    "firefox": ["firefox", "firefox-esr"],
    "chrome": ["google-chrome", "google-chrome-stable"],
    "edge": ["microsoft-edge", "microsoft-edge-stable"],
    "chromium": ["chromium", "chromium-browser"],
    "brave": ["brave-browser", "brave"],
}


def find(name: str) -> str | None:
    if WINDOWS:
        cands = _win_candidates(name)
    elif MACOS:
        cands = _mac_candidates(name)
    else:
        for cmd in LINUX_COMMANDS[name]:
            exe = shutil.which(cmd)
            if exe:
                return exe
        return None
    for c in cands:
        if c.exists():
            return str(c)
    return None


def detect() -> dict[str, str]:
    return {n: p for n in NAMES if (p := find(n))}


def profile_dir(name: str, exe: str) -> Path:
    # Snap-confined browsers cannot read hidden directories in $HOME.
    real = os.path.realpath(exe)
    if not (WINDOWS or MACOS) and (real.startswith("/snap/") or exe.startswith("/snap/")):
        snap = "firefox" if name == "firefox" else "chromium"
        return Path.home() / "snap" / snap / "common" / "hoppla-profile"
    return config_dir() / "profiles" / name


def pac_script(cfg: Config, port: int) -> str:
    conds = ["isPlainHostName(host)"]
    for d in cfg.psi_domains:
        d = d.strip().lstrip(".").lower()
        if d:
            conds += [f'host == "{d}"', f'dnsDomainIs(host, ".{d}")']
    for net in cfg.psi_networks:
        net = net.strip()
        if net:
            conds.append(f'shExpMatch(host, "{net}*")')
    proxy = f"SOCKS5 127.0.0.1:{port}; SOCKS 127.0.0.1:{port}"
    cond = " ||\n      ".join(conds)
    return ("function FindProxyForURL(url, host) {\n"
            "  host = host.toLowerCase();\n"
            f"  if ({cond})\n"
            f'    return "{proxy}";\n'
            '  return "DIRECT";\n'
            "}\n")


def _firefox_prefs(cfg: Config, port: int, pac_url: str | None, url: str) -> str:
    prefs = {
        "network.proxy.socks": "127.0.0.1",
        "network.proxy.socks_port": port,
        "network.proxy.socks_version": 5,
        "network.proxy.socks_remote_dns": True,
        "network.proxy.socks5_remote_dns": True,
        "network.proxy.no_proxies_on": "",
        "network.trr.mode": 5,  # no DNS-over-HTTPS; names must be resolved inside PSI
        "browser.startup.homepage": url,
        "browser.shell.checkDefaultBrowser": False,
        "browser.aboutwelcome.enabled": False,
        "datareporting.policy.dataSubmissionPolicyBypassNotification": True,
        "toolkit.telemetry.reportingpolicy.firstRun": False,
        "trailhead.firstrun.didSeeAboutWelcome": True,
    }
    if pac_url:
        prefs.update({"network.proxy.type": 2, "network.proxy.autoconfig_url": pac_url})
    else:
        prefs["network.proxy.type"] = 1
    lines = []
    for k, v in prefs.items():
        val = "true" if v is True else "false" if v is False else str(v) if isinstance(v, int) else f'"{v}"'
        lines.append(f'user_pref("{k}", {val});')
    return "// Written by hoppla on every launch; edits here are overwritten.\n" + "\n".join(lines) + "\n"


def command(cfg: Config, name: str, exe: str, port: int, url: str | None = None,
            pac_only_psi: bool | None = None) -> list[str]:
    url = url or cfg.start_url
    pac = cfg.pac_only_psi if pac_only_psi is None else pac_only_psi
    prof = profile_dir(name, exe)
    prof.mkdir(parents=True, exist_ok=True)
    pac_text = pac_script(cfg, port) if pac else None
    if name == "firefox":
        pac_url = None
        if pac_text:
            pac_file = prof / "psi-proxy.pac"
            pac_file.write_text(pac_text, encoding="utf-8")
            pac_url = pac_file.as_uri()
        (prof / "user.js").write_text(_firefox_prefs(cfg, port, pac_url, url), encoding="utf-8")
        return [exe, "-profile", str(prof), url]
    cmd = [exe, f"--user-data-dir={prof}", "--no-first-run", "--no-default-browser-check"]
    if pac_text:
        b64 = base64.b64encode(pac_text.encode()).decode()
        cmd.append(f"--proxy-pac-url=data:application/x-ns-proxy-autoconfig;base64,{b64}")
    else:
        cmd += [f"--proxy-server=socks5://127.0.0.1:{port}",
                "--host-resolver-rules=MAP * ~NOTFOUND , EXCLUDE 127.0.0.1"]
    return cmd + [url]


def launch(cfg: Config, name: str, port: int, url: str | None = None, pac_only_psi: bool | None = None) -> None:
    exe = find(name)
    if not exe:
        raise FileNotFoundError(f"{NAMES.get(name, name)} was not found on this computer.")
    detached_popen(command(cfg, name, exe, port, url, pac_only_psi))
