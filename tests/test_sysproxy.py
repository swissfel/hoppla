import json
import os
import stat
import sys
import tempfile
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

from hoppla import browsers, config, sysproxy

# A fake `networksetup` that keeps its state in a JSON file -- never the real one.
FAKE = r'''#!{python}
import json, os, sys
f = os.environ["FAKE_NETSETUP_STATE"]
st = json.load(open(f))
a = sys.argv[1:]
if a[0] == "-listallnetworkservices":
    print("An asterisk (*) denotes that a network service is disabled.")
    for s in st: print(("*" if st[s].get("disabled") else "") + s)
elif a[0] == "-getautoproxyurl":
    s = st[a[1]]; print("URL: " + (s["url"] or "(null)")); print("Enabled: " + ("Yes" if s["enabled"] else "No"))
elif a[0] == "-setautoproxyurl":
    st[a[1]]["url"] = a[2]
elif a[0] == "-setautoproxystate":
    st[a[1]]["enabled"] = a[2] == "on"
json.dump(st, open(f, "w"))
'''


@unittest.skipIf(sys.platform.startswith("win"), "fake networksetup is a script")
class TestSystemProxy(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.state = self.tmp / "netsetup.json"
        self.original = {"Wi-Fi": {"url": "http://corp.example/proxy.pac", "enabled": True},
                         "Ethernet": {"url": "", "enabled": False},
                         "Old VPN": {"url": "", "enabled": False, "disabled": True}}
        self.state.write_text(json.dumps(self.original))
        exe = self.tmp / "networksetup"
        exe.write_text(FAKE.format(python=sys.executable))
        exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
        self.env = mock.patch.dict(os.environ, {"PATH": f"{self.tmp}{os.pathsep}{os.environ['PATH']}",
                                                "FAKE_NETSETUP_STATE": str(self.state),
                                                "HOPPLA_HOME": str(self.tmp / "cfg")})
        self.env.start()
        (self.tmp / "cfg").mkdir()

    def tearDown(self):
        self.env.stop()

    def now(self):
        return json.loads(self.state.read_text())

    def assertRestored(self):
        """Every service is on/off as before, and URLs that were set are back."""
        now = self.now()
        for s, old in self.original.items():
            self.assertEqual(now[s]["enabled"], old["enabled"], s)
            if old["url"]:
                self.assertEqual(now[s]["url"], old["url"], s)

    def test_on_off_restores_exactly(self):
        cfg = config.Config(user="u")
        pac = sysproxy.PacServer(browsers.pac_script(cfg, 8989))
        try:
            self.assertEqual(sysproxy.services(), ["Wi-Fi", "Ethernet"])  # disabled service left alone
            sysproxy.enable(pac.url)
            for s in ("Wi-Fi", "Ethernet"):
                self.assertEqual(self.now()[s], {"url": pac.url, "enabled": True})
            text = urllib.request.urlopen(pac.url, timeout=5).read().decode()
            self.assertIn("SOCKS5 127.0.0.1:8989", text)
            self.assertIn('return "DIRECT"', text)  # everything that is not PSI stays direct
            self.assertTrue(sysproxy.restore())
            self.assertRestored()
            self.assertFalse(sysproxy.state_file().exists())
        finally:
            pac.close()

    def test_cleanup_after_crash(self):
        sysproxy.enable("http://127.0.0.1:1/hoppla.pac")  # session crashed before restoring
        self.assertNotEqual(self.now(), self.original)
        self.assertTrue(sysproxy.cleanup_leftovers())
        self.assertRestored()
        self.assertFalse(sysproxy.cleanup_leftovers())  # nothing left to do

    def test_second_enable_keeps_original(self):
        sysproxy.enable("http://127.0.0.1:1/a.pac")
        sysproxy.enable("http://127.0.0.1:2/b.pac")  # e.g. crash, then a new session before cleanup
        sysproxy.restore()
        self.assertRestored()


if __name__ == "__main__":
    unittest.main()
