import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hoppla import askpass, keyring, tunnel

# A fake `secret-tool` keeping its "keyring" in a JSON file -- never the real keyring.
FAKE = r'''#!{python}
import json, os, sys
f = os.environ["FAKE_KEYRING"]
ring = json.load(open(f)) if os.path.exists(f) else {{}}
a = sys.argv[1:]
acct = a[a.index("account") + 1]
if a[0] == "store":
    ring[acct] = sys.stdin.read()
elif a[0] == "lookup":
    if acct not in ring: sys.exit(1)
    sys.stdout.write(ring[acct])
elif a[0] == "clear":
    ring.pop(acct, None)
json.dump(ring, open(f, "w"))
'''


@unittest.skipUnless(sys.platform.startswith("linux"), "fake secret-tool is for Linux")
class TestRememberedPasswords(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.ring = self.tmp / "ring.json"
        exe = self.tmp / "secret-tool"
        exe.write_text(FAKE.format(python=sys.executable))
        exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
        (self.tmp / "cfg").mkdir()
        self.env = mock.patch.dict(os.environ, {
            "PATH": f"{self.tmp}{os.pathsep}{os.environ['PATH']}", "FAKE_KEYRING": str(self.ring),
            "HOPPLA_HOME": str(self.tmp / "cfg"), "HOPPLA_LOGIN_LABEL": "PSI gateway (hopx.psi.ch)",
            "HOPPLA_LOGIN_ACCOUNT": "u@hopx.psi.ch"})
        self.env.start()
        self.dialogs = []
        self.typed = []  # what the "user" types in the next dialogs
        self.remember = True

        def fake_ask(prompt, label="", root=None, remember=None):
            self.dialogs.append(prompt)
            if remember is not None:
                remember["value"] = self.remember
            return self.typed.pop(0)
        self.ask = mock.patch.object(askpass, "ask", fake_ask)
        self.ask.start()
        self.out = mock.patch.object(askpass, "_reply", lambda a: self.replies.append(a) or 0)
        self.replies = []
        self.out.start()

    def tearDown(self):
        self.out.stop()
        self.ask.stop()
        self.env.stop()

    def test_keyring_roundtrip(self):
        self.assertTrue(keyring.available())
        self.assertTrue(keyring.put("a@b", "pässwörd"))
        self.assertEqual(keyring.get("a@b"), "pässwörd")
        keyring.remove("a@b")
        self.assertIsNone(keyring.get("a@b"))

    def test_typo_then_remembered(self):
        self.typed = ["secre", "secret"]
        askpass.main(["Password:"])          # typo (stored for now, as "remember" is ticked)
        askpass.main(["Password:"])          # second try
        self.assertIn("attempt 2 of 3", self.dialogs[1])
        self.assertEqual(self.replies, ["secre", "secret"])
        self.assertEqual(keyring.get("u@hopx.psi.ch"), "secret")

    def test_saved_password_used_without_dialog(self):
        keyring.put("u@hopx.psi.ch", "secret")
        askpass.main(["Password:"])
        self.assertEqual(self.dialogs, [])
        self.assertEqual(self.replies, ["secret"])

    def test_outdated_saved_password_is_removed(self):
        keyring.put("u@hopx.psi.ch", "old")
        self.remember = False
        self.typed = ["new"]
        askpass.main(["Password:"])          # saved one, silently
        askpass.main(["Password:"])          # it failed: removed, dialog
        self.assertEqual(self.replies, ["old", "new"])
        self.assertIn("attempt 2 of 3", self.dialogs[0])
        self.assertIsNone(keyring.get("u@hopx.psi.ch"))

    def test_failed_login_does_not_keep_the_password(self):
        self.typed = ["wrong"]
        askpass.main(["Password:"])
        self.assertEqual(keyring.get("u@hopx.psi.ch"), "wrong")
        tunnel.forget_unconfirmed("u@hopx.psi.ch")   # what the session does when the login fails
        self.assertIsNone(keyring.get("u@hopx.psi.ch"))

    def test_mfa_code_is_never_stored(self):
        self.typed = ["123456"]
        askpass.main(["Verification code:"])
        self.assertFalse(self.ring.exists() and json.loads(self.ring.read_text()))


if __name__ == "__main__":
    unittest.main()
