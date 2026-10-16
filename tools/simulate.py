"""Run a Hoppla session against tools/fake_hopx.py with scripted answers to the dialogs.

    python tools/fake_hopx.py &        # in another terminal (needs paramiko)
    python tools/simulate.py           # connects, checks proxy + forward, disconnects

Uses a throw-away settings folder; your real settings and the real hopx are not touched.
The password/MFA/yes-no dialogs are answered by a small script instead of a person;
set HOPPLA_SIM_PASSWORD to a wrong value to see the failed-login handling.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

ANSWERS = '''#!{python}
import os, sys
prompt = " ".join(sys.argv[1:]).lower()
with open(os.environ["HOPPLA_HOME"] + "/prompts.txt", "a") as f:
    f.write(" ".join(sys.argv[1:]).replace("\\n", " | ") + "\\n")
if "y/n" in prompt or "yes/no" in prompt:
    print("yes")
elif "password" in prompt:
    print(os.environ.get("HOPPLA_SIM_PASSWORD", "secret"))
else:
    print("123456")
'''


def _log(home: Path) -> str:
    log = home / "session.log"
    return log.read_text(errors="replace") if log.exists() else "(no session.log)"


def main() -> int:
    home = Path(tempfile.mkdtemp(prefix="hoppla-sim-"))
    os.environ["HOPPLA_HOME"] = str(home)
    from hoppla import config, tunnel

    cfg = config.Config(user="tester", gateway="127.0.0.1", gateway_port=2022, login_mode="app")
    cfg.service("jupyter-ra").enabled = True
    for s in cfg.services:  # own ports, so a real Hoppla connection can stay open meanwhile
        s.local_port += 10000
    cfg.save()
    answers = home / "answers.py"
    answers.write_text(ANSWERS.format(python=sys.executable))
    answers.chmod(0o755)

    # the runner process: Hoppla's own background session, with the dialog replaced by the script
    runner = subprocess.Popen([sys.executable, "-c", (
        "import sys; sys.path.insert(0, %r)\n"
        "from hoppla import tunnel\n"
        "tunnel.askpass_program = lambda: %r\n"
        "sys.exit(tunnel.session_main([], background=True))") % (str(ROOT), str(answers))])

    ok = True
    try:
        socks, jup = cfg.service("socks-ra"), cfg.service("jupyter-ra")
        for _ in range(60):
            if tunnel.read_state() and tunnel.listening(socks.local_port) and tunnel.listening(jup.local_port):
                break
            if runner.poll() is not None:
                break
            time.sleep(0.5)
        print("result:", tunnel.last_result().get("message"))
        print("prompts shown:", (home / "prompts.txt").read_text().strip().splitlines()
              if (home / "prompts.txt").exists() else [])
        if runner.poll() is not None:
            print("session ended:", tunnel.last_result())
            print(_log(home)[-1500:])
            return 1
        out = subprocess.run(["curl", "-s", "--max-time", "10", "--socks5-hostname",
                              f"127.0.0.1:{socks.local_port}", "http://intranet.psi.ch/test"],
                             capture_output=True, text=True)
        print("via SOCKS proxy on Ra:", out.stdout.strip() or out.stderr.strip())
        ok &= "inside PSI" in out.stdout
        page = urllib.request.urlopen(f"http://127.0.0.1:{jup.local_port}/lab", timeout=10).read().decode()
        print("via Jupyter forward:  ", page.strip())
        ok &= "inside PSI" in page
    finally:
        tunnel.stop()
        time.sleep(1)
        print("after stop:", tunnel.last_result().get("message"), "| still running:", bool(tunnel.read_state()))
        print("session.log:\n" + _log(home).strip()[-800:])
    print("OK" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
