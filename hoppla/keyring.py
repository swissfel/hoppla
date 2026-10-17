"""Remembered passwords in the operating system's keyring, without extra packages.

Linux: Secret Service (GNOME Keyring, KWallet) via `secret-tool`; macOS: Keychain via
`security`; Windows: Credential Manager via the Windows API. Passwords never appear on a
command line (where other users could see them) and are never written to Hoppla's files.
"""

from __future__ import annotations

import shutil
import subprocess

from .config import MACOS, WINDOWS

SERVICE = "hoppla"


def available() -> bool:
    if WINDOWS:
        return True
    if MACOS:
        return shutil.which("security") is not None
    return shutil.which("secret-tool") is not None


def get(account: str) -> str | None:
    try:
        if WINDOWS:
            return _win_read(account)
        if MACOS:
            out = _run(["security", "find-generic-password", "-s", SERVICE, "-a", account, "-w"])
        else:
            out = _run(["secret-tool", "lookup", "service", SERVICE, "account", account])
        if out.returncode == 0 and out.stdout:
            return out.stdout.rstrip("\n")
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def put(account: str, password: str) -> bool:
    try:
        if WINDOWS:
            return _win_write(account, password)
        if MACOS:
            # commands read from stdin (`security -i`), so the password is not visible in the process list
            line = f'add-generic-password -U -s "{SERVICE}" -a "{_q(account)}" -w "{_q(password)}"\n'
            out = _run(["security", "-i"], input=line)
        else:
            out = _run(["secret-tool", "store", "--label", f"Hoppla: {account}", "service", SERVICE,
                        "account", account], input=password)
        return out.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def remove(account: str) -> None:
    try:
        if WINDOWS:
            _win_delete(account)
        elif MACOS:
            _run(["security", "delete-generic-password", "-s", SERVICE, "-a", account])
        else:
            _run(["secret-tool", "clear", "service", SERVICE, "account", account])
    except (OSError, subprocess.SubprocessError):
        pass


def _q(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _run(cmd: list[str], input: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, input=input, capture_output=True, text=True, timeout=60)


# ------------------------------------------------------------- Windows -------
def _win_target(account: str) -> str:
    return f"{SERVICE}:{account}"


def _win_api():
    import ctypes
    from ctypes import wintypes

    class CREDENTIAL(ctypes.Structure):
        _fields_ = [("Flags", wintypes.DWORD), ("Type", wintypes.DWORD), ("TargetName", wintypes.LPWSTR),
                    ("Comment", wintypes.LPWSTR), ("LastWritten", wintypes.FILETIME),
                    ("CredentialBlobSize", wintypes.DWORD), ("CredentialBlob", ctypes.POINTER(ctypes.c_char)),
                    ("Persist", wintypes.DWORD), ("AttributeCount", wintypes.DWORD),
                    ("Attributes", ctypes.c_void_p), ("TargetAlias", wintypes.LPWSTR),
                    ("UserName", wintypes.LPWSTR)]

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    advapi.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                 ctypes.POINTER(ctypes.POINTER(CREDENTIAL))]
    advapi.CredWriteW.argtypes = [ctypes.POINTER(CREDENTIAL), wintypes.DWORD]
    advapi.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
    advapi.CredFree.argtypes = [ctypes.c_void_p]
    return ctypes, CREDENTIAL, advapi


CRED_TYPE_GENERIC, CRED_PERSIST_LOCAL_MACHINE = 1, 2


def _win_read(account: str) -> str | None:
    ctypes, CREDENTIAL, advapi = _win_api()
    p = ctypes.POINTER(CREDENTIAL)()
    if not advapi.CredReadW(_win_target(account), CRED_TYPE_GENERIC, 0, ctypes.byref(p)):
        return None
    try:
        c = p.contents
        return ctypes.string_at(c.CredentialBlob, c.CredentialBlobSize).decode("utf-16-le")
    finally:
        advapi.CredFree(p)


def _win_write(account: str, password: str) -> bool:
    ctypes, CREDENTIAL, advapi = _win_api()
    blob = password.encode("utf-16-le")
    c = CREDENTIAL()
    c.Type = CRED_TYPE_GENERIC
    c.TargetName = _win_target(account)
    c.UserName = account
    c.CredentialBlobSize = len(blob)
    c.CredentialBlob = ctypes.cast(ctypes.create_string_buffer(blob, len(blob)), ctypes.POINTER(ctypes.c_char))
    c.Persist = CRED_PERSIST_LOCAL_MACHINE
    return bool(advapi.CredWriteW(ctypes.byref(c), 0))


def _win_delete(account: str) -> None:
    _, _, advapi = _win_api()
    advapi.CredDeleteW(_win_target(account), CRED_TYPE_GENERIC, 0)
