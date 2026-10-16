"""Build distributables.

    python build.py zipapp     -> dist/hoppla.pyz (any OS with Python >= 3.9)
    python build.py binary     -> single binary for THIS OS via PyInstaller
                                  (Windows: hoppla.exe, macOS: Hoppla.app zipped, Linux: hoppla)

PyInstaller cannot cross-compile; build each OS on that OS (see .github/workflows/build.yml).
"""

import platform
import shutil
import subprocess
import sys
import zipapp
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist"


def build_zipapp() -> Path:
    stage = ROOT / "build" / "zipapp"
    shutil.rmtree(stage, ignore_errors=True)
    shutil.copytree(ROOT / "hoppla", stage / "hoppla", ignore=shutil.ignore_patterns("__pycache__"))
    DIST.mkdir(exist_ok=True)
    out = DIST / "hoppla.pyz"
    zipapp.create_archive(stage, out, interpreter="/usr/bin/env python3", main="hoppla.cli:main")
    return out


def build_binary() -> Path:
    system = platform.system()
    name = "Hoppla" if system == "Darwin" else "hoppla"
    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--name", name,
           "--paths", str(ROOT), "--distpath", str(DIST), "--workpath", str(ROOT / "build" / "pyi"),
           "--specpath", str(ROOT / "build")]
    if system == "Windows":
        cmd += ["--onefile", "--windowed"]  # the session allocates its own console
    elif system == "Darwin":
        cmd += ["--windowed", "--osx-bundle-identifier", "ch.psi.hoppla"]  # .app bundle
    else:
        cmd += ["--onefile"]
    subprocess.check_call(cmd + [str(ROOT / "packaging" / "entry.py")])

    arch = platform.machine().lower().replace("amd64", "x86_64").replace("aarch64", "arm64")
    if system == "Darwin":
        out = DIST / f"Hoppla-macos-{arch}.zip"
        subprocess.check_call(["ditto", "-c", "-k", "--keepParent", str(DIST / f"{name}.app"), str(out)])
    elif system == "Windows":
        out = DIST / f"hoppla-windows-{arch}.exe"
        shutil.move(DIST / f"{name}.exe", out)
    else:
        out = DIST / f"hoppla-linux-{arch}"
        shutil.move(DIST / name, out)
    return out


if __name__ == "__main__":
    what = sys.argv[1] if len(sys.argv) > 1 else "zipapp"
    print({"zipapp": build_zipapp, "binary": build_binary}[what]())
