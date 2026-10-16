"""Operating-system specific helpers (terminals, processes, ssh lookup)."""

from __future__ import annotations

import os
import shlex
import shutil
import signal
import subprocess
import sys
from pathlib import Path

from .config import MACOS, WINDOWS, config_dir

PACKAGE_DIR = Path(__file__).resolve().parent
EXECUTABLE: str | None = None  # set after installing the frozen binary elsewhere


class PlatformError(RuntimeError):
    pass


# ---------------------------------------------------------------- ssh -------
def find_ssh() -> str | None:
    found = shutil.which("ssh")
    if found:
        return found
    if WINDOWS:
        root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
        for sub in ("System32", "Sysnative"):
            p = root / sub / "OpenSSH" / "ssh.exe"
            if p.exists():
                return str(p)
    return None


def ssh_install_hint() -> str:
    if WINDOWS:
        return ("The OpenSSH client is missing. Install it via Settings > System > Optional features > "
                "Add a feature > 'OpenSSH Client', then restart this program.")
    if MACOS:
        return "ssh was not found, which is unusual on macOS. Check your PATH."
    return "ssh was not found. Install the OpenSSH client (e.g. 'sudo apt install openssh-client')."


# ----------------------------------------------------- our own command -----
def self_command(*args: str, gui: bool = False) -> list[str]:
    """Command line that re-invokes this program (frozen binary, zipapp or source)."""
    if getattr(sys, "frozen", False):
        return [EXECUTABLE or sys.executable, *args]
    py = python_exe(gui)
    argv0 = Path(sys.argv[0]).resolve() if sys.argv and sys.argv[0] else None
    if argv0 and argv0.suffix == ".pyz" and argv0.is_file():
        return [py, str(argv0), *args]
    return [py, str(PACKAGE_DIR / "__main__.py"), *args]


def python_exe(gui: bool) -> str:
    """On Windows pick python.exe for consoles and pythonw.exe for windowless runs."""
    exe = Path(sys.executable)
    if WINDOWS:
        want = "pythonw.exe" if gui else "python.exe"
        cand = exe.with_name(want)
        if cand.exists():
            return str(cand)
    return str(exe)


# ---------------------------------------------------------- processes -------
def pid_alive(pid: int) -> bool:
    if not pid or pid <= 0:
        return False
    if WINDOWS:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        kernel32.CloseHandle(handle)
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def kill_pid(pid: int) -> None:
    if not pid_alive(pid):
        return
    try:
        os.kill(pid, signal.SIGTERM)  # TerminateProcess on Windows
    except OSError:
        pass


def detached_popen(argv: list[str], **kw) -> subprocess.Popen:
    """Start a program that outlives us and does not tie up our console."""
    if WINDOWS:
        kw.setdefault("creationflags", subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)
    else:
        kw.setdefault("start_new_session", True)
    return subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, close_fds=True, **kw)


def ensure_console(title: str | None = None) -> None:
    """Give a windowless (pythonw / frozen --windowed) process a console on Windows."""
    if not WINDOWS:
        return
    import ctypes

    kernel32 = ctypes.windll.kernel32
    if not kernel32.GetConsoleWindow():
        kernel32.AllocConsole()
    if sys.stdout is None or sys.stdin is None or not _usable(sys.stdout):
        sys.stdin = open("CONIN$", "r")
        sys.stdout = open("CONOUT$", "w", buffering=1)
        sys.stderr = sys.stdout
    if title:
        kernel32.SetConsoleTitleW(title)


def _usable(stream) -> bool:
    try:
        stream.fileno()
        return True
    except (OSError, AttributeError, ValueError):
        return False


# ----------------------------------------------------------- terminals ------
LINUX_TERMINALS = [
    ("x-terminal-emulator", ["-e"]),
    ("gnome-terminal", ["--"]),
    ("konsole", ["-e"]),
    ("xfce4-terminal", ["-x"]),
    ("mate-terminal", ["-x"]),
    ("tilix", ["-e"]),
    ("kitty", []),
    ("alacritty", ["-e"]),
    ("foot", []),
    ("xterm", ["-e"]),
]


def open_in_terminal(argv: list[str], title: str = "Hoppla") -> None:
    """Run `argv` in a new, visible terminal window (needed for password/OTP prompts)."""
    if WINDOWS:
        if Path(argv[0]).name.lower() == "pythonw.exe":
            argv = [str(Path(argv[0]).with_name("python.exe")), *argv[1:]]
        subprocess.Popen(argv, creationflags=subprocess.CREATE_NEW_CONSOLE, close_fds=True)
        return
    if MACOS:
        script = config_dir() / "launch" / (_slug(title) + ".command")
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text(f"#!/bin/sh\nprintf '\\033]0;%s\\007' {shlex.quote(title)}\nexec {shlex.join(argv)}\n")
        script.chmod(0o755)
        subprocess.Popen(["open", "-a", "Terminal", str(script)])
        return
    candidates = []
    env_term = os.environ.get("TERMINAL")
    if env_term:
        candidates.append((env_term, ["-e"]))
    candidates += LINUX_TERMINALS
    for name, flag in candidates:
        exe = shutil.which(name)
        if exe:
            detached_popen([exe, *flag, *argv])
            return
    raise PlatformError("No terminal program found. Run 'hoppla start --here' in a terminal instead.")


def _slug(text: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in text.lower()).strip("-")


# ------------------------------------------------------------- opening ------
def open_path(target: str) -> None:
    """Open a file or URL with the system default handler."""
    if WINDOWS:
        os.startfile(target)  # type: ignore[attr-defined]
    elif MACOS:
        subprocess.Popen(["open", target])
    else:
        detached_popen(["xdg-open", target])


def desktop_dir() -> Path:
    if WINDOWS:
        try:
            out = subprocess.run(["powershell", "-NoProfile", "-Command", "[Environment]::GetFolderPath('Desktop')"],
                                 capture_output=True, text=True, timeout=20,
                                 creationflags=subprocess.CREATE_NO_WINDOW)
            if out.stdout.strip():
                return Path(out.stdout.strip())
        except (OSError, subprocess.SubprocessError):
            pass
    elif not MACOS and shutil.which("xdg-user-dir"):
        out = subprocess.run(["xdg-user-dir", "DESKTOP"], capture_output=True, text=True)
        if out.stdout.strip():
            return Path(out.stdout.strip())
    return Path.home() / "Desktop"
