"""Desktop / start-menu shortcuts that run hoppla commands."""

from __future__ import annotations

import base64
import os
import plistlib
import re
import shlex
import shutil
import subprocess
from pathlib import Path

from .config import APP_TITLE, MACOS, WINDOWS
from .platforms import desktop_dir, self_command

LINUX_ICONS = {"gui": "network-vpn", "tunnel": "network-vpn", "browser": "web-browser",
               "ssh": "utilities-terminal", "rdp": "preferences-desktop-remote-desktop"}


def create(label: str, args: list[str], kind: str = "tunnel", desktop: bool = True) -> list[Path]:
    """Create a launcher named `label` running `hoppla <args>`; returns created paths."""
    argv = self_command(*args, gui=True)
    if WINDOWS:
        return _windows(label, argv, desktop)
    if MACOS:
        return _macos(label, argv, desktop)
    return _linux(label, argv, kind, desktop)


def _safe_name(label: str) -> str:
    return re.sub(r'[<>:"/\\|?*]+', "-", label).strip()


# --------------------------------------------------------------- Windows ----
def _ps_quote(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def _windows(label: str, argv: list[str], desktop: bool) -> list[Path]:
    target, args = argv[0], subprocess.list2cmdline(argv[1:])
    folders = ["(Join-Path ([Environment]::GetFolderPath('Programs')) 'Hoppla')"]
    if desktop:
        folders.insert(0, "[Environment]::GetFolderPath('Desktop')")
    script = f"""
$ErrorActionPreference = 'Stop'
$sh = New-Object -ComObject WScript.Shell
foreach ($dir in @({', '.join(folders)})) {{
  New-Item -ItemType Directory -Force -Path $dir | Out-Null
  $lnk = $sh.CreateShortcut((Join-Path $dir {_ps_quote(_safe_name(label) + '.lnk')}))
  $lnk.TargetPath = {_ps_quote(target)}
  $lnk.Arguments = {_ps_quote(args)}
  $lnk.WorkingDirectory = {_ps_quote(str(Path.home()))}
  $lnk.IconLocation = {_ps_quote(target + ',0')}
  $lnk.Description = {_ps_quote(APP_TITLE + ': ' + label)}
  $lnk.Save()
  Write-Output $lnk.FullName
}}
"""
    enc = base64.b64encode(script.encode("utf-16-le")).decode()
    out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", enc],
                         capture_output=True, text=True, timeout=60,
                         creationflags=subprocess.CREATE_NO_WINDOW)
    if out.returncode:
        raise RuntimeError("Creating the shortcut failed:\n" + (out.stderr or out.stdout))
    return [Path(line) for line in out.stdout.splitlines() if line.strip()]


# ----------------------------------------------------------------- macOS ----
def _macos(label: str, argv: list[str], desktop: bool) -> list[Path]:
    name = _safe_name(label)
    app = Path.home() / "Applications" / f"{name}.app"
    if app.exists():
        shutil.rmtree(app)
    macos = app / "Contents" / "MacOS"
    macos.mkdir(parents=True)
    exe = macos / "launcher"
    exe.write_text(f"#!/bin/sh\nexec {shlex.join(argv)}\n")
    exe.chmod(0o755)
    slug = re.sub(r"[^A-Za-z0-9]+", "-", name).strip("-").lower()
    with open(app / "Contents" / "Info.plist", "wb") as fh:
        plistlib.dump({
            "CFBundleName": name,
            "CFBundleDisplayName": name,
            "CFBundleIdentifier": f"ch.psi.hoppla.{slug}",
            "CFBundleExecutable": "launcher",
            "CFBundlePackageType": "APPL",
            "CFBundleShortVersionString": "1.0",
            "LSUIElement": True,  # no Dock icon for the tiny launcher itself
        }, fh)
    created = [app]
    if desktop:
        link = desktop_dir() / f"{name}.app"
        if link.is_symlink() or link.exists():
            link.unlink()
        link.symlink_to(app)
        created.append(link)
    return created


# ----------------------------------------------------------------- Linux ----
def _desktop_quote(arg: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_@%+=:,./-]+", arg):
        return arg.replace("%", "%%")
    escaped = re.sub(r'(["`$\\])', r"\\\1", arg).replace("%", "%%")
    return f'"{escaped}"'


def desktop_entry(label: str, argv: list[str], kind: str) -> str:
    return "\n".join([
        "[Desktop Entry]",
        "Type=Application",
        f"Name={label}",
        f"Comment={'Tunnels into PSI through hopx' if label == APP_TITLE else APP_TITLE + ': ' + label}",
        "Exec=" + " ".join(_desktop_quote(a) for a in argv),
        f"Icon={LINUX_ICONS.get(kind, 'network-vpn')}",
        "Terminal=false",
        "Categories=Network;",
        "",
    ])


def _linux(label: str, argv: list[str], kind: str, desktop: bool) -> list[Path]:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", label).strip("-").lower()
    text = desktop_entry(label, argv, kind)
    apps = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "applications"
    targets = [apps / f"hoppla-{slug}.desktop"]
    if desktop:
        targets.append(desktop_dir() / f"hoppla-{slug}.desktop")
    for t in targets:
        t.parent.mkdir(parents=True, exist_ok=True)
        t.write_text(text, encoding="utf-8")
        t.chmod(0o755)
        if shutil.which("gio"):  # GNOME: mark as trusted so it can be double-clicked
            subprocess.run(["gio", "set", str(t), "metadata::trusted", "true"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return targets
