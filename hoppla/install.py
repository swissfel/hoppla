"""Copy the downloaded binary to a permanent per-user place and add a menu entry.

Desktop icons point to the program file, so it must not stay in Downloads.
macOS users drag the .app to Applications instead; nothing to do there.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from . import platforms, shortcuts
from .config import APP_TITLE, MACOS, WINDOWS


def target() -> Path | None:
    if not getattr(sys, "frozen", False) or MACOS:
        return None
    if WINDOWS:
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        return base / "Programs" / "Hoppla" / "hoppla.exe"
    return Path.home() / ".local" / "bin" / "hoppla"


def is_installed() -> bool:
    t = target()
    return t is None or Path(sys.executable).resolve() == t.resolve()


def install() -> list[Path]:
    t = target()
    if t is None:
        return []
    t.parent.mkdir(parents=True, exist_ok=True)
    tmp = t.with_name(t.name + ".new")
    shutil.copy2(sys.executable, tmp)
    if not WINDOWS:
        tmp.chmod(0o755)
    os.replace(tmp, t)  # also works while an older copy is running (Linux)
    platforms.EXECUTABLE = str(t)  # shortcuts made from now on point to the installed copy
    return [t, *shortcuts.create(APP_TITLE, [], "gui")]
