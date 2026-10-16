import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hoppla import actions, askpass, browsers, config, shortcuts, tunnel


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        env = {"HOPPLA_HOME": str(self.home / "cfg"), "HOME": str(self.home),
               "USERPROFILE": str(self.home), "XDG_DATA_HOME": str(self.home / "share")}
        self.env = mock.patch.dict(os.environ, env)
        self.env.start()
        self.cfg = config.Config(user="tester")

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()


class TestConfig(Base):
    def test_roundtrip(self):
        self.cfg.services[1].enabled = True
        self.cfg.save()
        loaded = config.load()
        self.assertEqual(loaded.to_dict(), self.cfg.to_dict())

    def test_inner_socks_pulls_in_its_ssh_host(self):
        self.cfg.service("socks-ra").enabled = True
        plan = tunnel.make_plan(self.cfg)
        self.assertIn("ssh-ra", [s.id for s in plan.gateway])
        self.assertEqual([s.id for s in plan.inner["ssh-ra"]], ["socks-ra"])

    def test_problems(self):
        self.cfg.user = ""
        self.cfg.services[1].local_port = self.cfg.services[0].local_port
        self.cfg.services[1].enabled = True
        probs = self.cfg.problems()
        self.assertTrue(any("user" in p for p in probs))
        self.assertTrue(any("also used" in p for p in probs))

    def test_migrates_old_configs(self):
        v1 = {"user": "u", "version": 1, "services": [{"id": "x", "name": "x", "type": "socks", "local_port": 1}]}
        self.assertEqual([s.id for s in config.Config.from_dict(v1).services], [s.id for s in config.presets()])
        v2 = {"user": "u", "version": 2, "services": [
            {"id": "rdp-winterm3", "name": "RDP", "type": "forward", "local_port": 9022,
             "remote_host": "winterm3.psi.ch", "remote_port": 3389}]}
        self.assertEqual(config.Config.from_dict(v2).services[0].remote_host, "winterm6.psi.ch")

    def test_new_id_unique(self):
        self.assertEqual(self.cfg.new_id("SSH to Ra"), "ssh-to-ra")
        self.cfg.services.append(config.Service("ssh-to-ra", "x", "ssh", 2300, "h"))
        self.assertEqual(self.cfg.new_id("SSH to Ra"), "ssh-to-ra-2")


class TestCommands(Base):
    def test_gateway_command_single_connection(self):
        services = self.cfg.selected(["ssh-ra", "rdp-winterm", "socks"])
        cmd = tunnel.gateway_command(self.cfg, services, "ssh")
        self.assertEqual(cmd[-1], "tester@hopx.psi.ch")
        self.assertIn("127.0.0.1:1080", cmd)
        self.assertIn("127.0.0.1:2201:ra.psi.ch:22", cmd)
        self.assertIn("127.0.0.1:9022:winterm6.psi.ch:3389", cmd)

    def test_inner_connection_carries_proxy_and_forward(self):
        self.cfg.service("jupyter-ra").enabled = True
        plan = tunnel.make_plan(self.cfg)
        self.assertEqual([s.id for s in plan.gateway], ["ssh-ra"])
        self.assertEqual([s.id for s in plan.inner["ssh-ra"]], ["socks-ra", "jupyter-ra"])
        cmd = tunnel.inner_command(self.cfg, self.cfg.service("ssh-ra"), plan.inner["ssh-ra"], "ssh")
        self.assertEqual(cmd[-3:], ["-p", "2201", "tester@127.0.0.1"])
        self.assertIn("HostKeyAlias=ra.psi.ch", cmd)
        self.assertIn("127.0.0.1:8989", cmd)
        self.assertIn("127.0.0.1:9789:jupytera.psi.ch:443", cmd)

    def test_gateway_is_interactive_setup_connection(self):
        cmd = tunnel.gateway_command(self.cfg, tunnel.make_plan(self.cfg).gateway, "ssh")
        self.assertIn("-tt", cmd)
        self.assertNotIn("-N", cmd)
        self.assertIn("PreferredAuthentications=keyboard-interactive", cmd)
        self.assertIn("NumberOfPasswordPrompts=1", cmd)

    def test_cooldown(self):
        tunnel._write_result("", attempt=__import__("time").time())
        self.assertGreater(tunnel.cooldown_left(), 20)
        tunnel._write_result("", attempt=0)
        self.assertEqual(tunnel.cooldown_left(), 0)

    def test_ssh_config_block_replaced_not_duplicated(self):
        path = actions.ssh_config_path()
        path.parent.mkdir(parents=True)
        path.write_text("Host mine\n  HostName example.org\n")
        actions.write_ssh_config(self.cfg)
        actions.write_ssh_config(self.cfg)
        text = path.read_text()
        self.assertEqual(text.count(actions.BEGIN), 1)
        self.assertIn("Host psi-ra", text)
        self.assertIn("Host mine", text)
        actions.write_ssh_config(self.cfg, remove=True)
        self.assertEqual(path.read_text(), "Host mine\n  HostName example.org\n")


class TestIndependence(Base):
    def test_ignores_user_ssh_config(self):
        cmd = tunnel.gateway_command(self.cfg, self.cfg.selected(), "ssh")
        self.assertEqual(cmd[1:3], ["-F", "none"])
        self.assertIn("StrictHostKeyChecking=accept-new", cmd)
        self.assertTrue(any(a.startswith("UserKnownHostsFile=") for a in cmd))

    def test_explain(self):
        self.assertIn("password or MFA", tunnel.explain("x@hopx: Permission denied (keyboard-interactive)."))
        self.assertIn("internet", tunnel.explain("ssh: Could not resolve hostname hopx.psi.ch"))

    def test_askpass_prompts(self):
        self.assertEqual(askpass.classify("Password: "), "secret")
        self.assertEqual(askpass.classify("Verification code: "), "code")
        self.assertEqual(askpass.classify("continue connecting (yes/no/[fingerprint])?"), "confirm")
        self.assertIn("@ra.psi.ch", askpass.friendly("u@127.0.0.1's password:", "ra.psi.ch"))


class TestBrowsers(Base):
    def test_chrome_all_traffic(self):
        cmd = browsers.command(self.cfg, "chrome", "/opt/chrome", 1080, pac_only_psi=False)
        self.assertIn("--proxy-server=socks5://127.0.0.1:1080", cmd)
        self.assertTrue(any(a.startswith("--user-data-dir=") for a in cmd))

    def test_chrome_pac(self):
        cmd = browsers.command(self.cfg, "chrome", "/opt/chrome", 1080, pac_only_psi=True)
        self.assertTrue(any(a.startswith("--proxy-pac-url=data:") for a in cmd))

    def test_firefox_profile(self):
        cmd = browsers.command(self.cfg, "firefox", "/opt/firefox", 1081, pac_only_psi=False)
        userjs = (Path(cmd[2]) / "user.js").read_text()
        self.assertIn('"network.proxy.socks_port", 1081', userjs)
        self.assertIn('"network.proxy.socks_remote_dns", true', userjs)

    def test_pac(self):
        pac = browsers.pac_script(self.cfg, 1080)
        self.assertIn('dnsDomainIs(host, ".psi.ch")', pac)
        self.assertIn("SOCKS5 127.0.0.1:1080", pac)


class TestShortcuts(Base):
    def test_desktop_quoting(self):
        entry = shortcuts.desktop_entry("X", ["/a b/py", "run", "100%"], "tunnel")
        self.assertIn('Exec="/a b/py" run 100%%', entry)


if __name__ == "__main__":
    unittest.main()
