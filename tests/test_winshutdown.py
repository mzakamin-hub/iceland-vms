"""Unit tests (PC) for 0.2-21 WA-7: the Windows clean Shut Down (ssh identities, the one-time app-key install,
the power-key fallback). Pure logic + a socket pair: no VM, no ssh."""
import base64
import os
import socket
import struct
import sys
import tempfile
import time
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from iceland_vms import config, crosvmargs, helper  # noqa: E402

from test_core import RES  # noqa: E402

PUB = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAbCdEfGhIjKlMnOpQrStUvWxYz0123456789abcdefghij iceland-vms\n"


def decoded(cmd):
    head = "powershell -NoProfile -NonInteractive -EncodedCommand "
    assert cmd.startswith(head)
    return base64.b64decode(cmd[len(head):]).decode("utf-16-le")


class Identities(unittest.TestCase):
    def test_argv_offers_every_key_in_order(self):
        a = helper.win_ssh_argv(["/h/app", "/h/.ssh/id_ed25519"], "gvm", "10.0.0.2", "/h/kh", "shutdown /s /t 0")
        self.assertIn("IdentitiesOnly=yes", a)
        self.assertEqual([a[i + 1] for i, x in enumerate(a) if x == "-i"], ["/h/app", "/h/.ssh/id_ed25519"])
        self.assertEqual(a[-2:], ["gvm@10.0.0.2", "shutdown /s /t 0"])
        self.assertIn("BatchMode=yes", a)
        self.assertIn("PasswordAuthentication=no", a)

    def test_identities_app_first_then_owner_defaults(self):
        with tempfile.TemporaryDirectory() as h:
            os.makedirs(os.path.join(h, ".ssh"))
            for n in ("id_rsa", "id_ed25519", "id_ed25519.pub"):
                open(os.path.join(h, ".ssh", n), "w").close()
            app = os.path.join(h, "app_key")
            self.assertEqual(helper.win_identities(h, app), (None, [os.path.join(h, ".ssh", "id_ed25519"),
                                                                    os.path.join(h, ".ssh", "id_rsa")]))
            open(app, "w").close()
            self.assertEqual(helper.win_identities(h, app)[0], app)

    def test_no_keys(self):
        with tempfile.TemporaryDirectory() as h:
            self.assertEqual(helper.win_identities(h, os.path.join(h, "none")), (None, []))


class KeyInstall(unittest.TestCase):
    def test_script(self):
        s = decoded(helper.win_key_install_cmd(PUB))
        self.assertIn("C:\\ProgramData\\ssh\\administrators_authorized_keys", s)
        self.assertIn("$k='%s'" % PUB.strip(), s)
        self.assertIn("/inheritance:r /grant '*S-1-5-32-544:F' /grant 'SYSTEM:F'", s)
        self.assertIn("PBUTTONACTION 3", s)
        self.assertTrue(s.rstrip().endswith("'%s'" % helper.WIN_KEY_OK))
        self.assertIn("-not $c.Contains($k)", s)              # idempotent: never a second copy

    def test_refuses_odd_input(self):
        for bad in ("", "not a key", "ssh-ed25519 AAA x'; Remove-Item C:\\", PUB + PUB, "-----BEGIN OPENSSH PRIVATE KEY"):
            self.assertRaises(helper.HelperError, helper.win_key_install_cmd, bad)

    def test_command_fits_cmd_exe(self):
        self.assertLess(len(helper.win_key_install_cmd(PUB)), 8000)


class PowerKey(unittest.TestCase):
    def test_events(self):
        b = helper.virtio_key_tap()
        self.assertEqual(len(b), 32)
        ev = [struct.unpack("<HHi", b[i:i + 8]) for i in range(0, 32, 8)]
        self.assertEqual(ev, [(1, 116, 1), (0, 0, 0), (1, 116, 0), (0, 0, 0)])

    def win_cfg(self):
        c = config.default_config("windows-11", "Windows 11")
        config.windows_defaults(c)
        c["disks"] = [{"store": "vm", "name": "disk0.raw", "kind": "disk", "ro": False}]
        return c

    def test_default_off_and_validated(self):
        c = self.win_cfg()
        self.assertFalse(c["windows"]["power_key"])             # 0.2-22: Windows ignores it (WP-5)
        config.validate(c)
        c["windows"]["power_key"] = "yes"
        self.assertRaises(config.ConfigError, config.validate, c)
        del c["windows"]["power_key"]                   # a 0.2-20 vm.json
        config.validate(c)

    def test_crosvm_argv(self):
        c = self.win_cfg()
        res = dict(RES, firmware=config.firmware_path(c), power_key="/proc/self/fd/9")
        a, _ = crosvmargs.build(c, res)
        i = a.index("--input")
        self.assertEqual(a[i + 1], "keyboard[path=/proc/self/fd/9]")
        self.assertEqual(a[-1], config.FIRMWARE_ECAM)       # the firmware stays the last argument
        res.pop("power_key")
        self.assertNotIn("--input", crosvmargs.build(c, res)[0])

    def agent(self, term=False):
        logs, states = [], []
        sup = types.SimpleNamespace(log=logs.append, state={"ip": "10.250.0.9", "state": "running"}, term=term,
                                    force_at=0.0, set_state=lambda st, msg=None: states.append((st, msg)),
                                    cfg={"network": {"mode": "off", "mac": "0a:1c:00:00:00:02"},
                                         "windows": {"ssh_user": "gvm"}})
        ag = helper.WinAgent(sup)
        ag.stop = True                                       # no polling thread work in the test
        ag.t.join(6)
        ag.states = states
        return ag, logs

    def test_no_ssh_presses_the_power_key(self):
        ag, logs = self.agent()
        ours, theirs = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        ag.pk = ours
        self.assertTrue(ag.send("ping"))                     # Shut Down is possible without ssh now
        self.assertTrue(ag.send("poweroff"))
        self.assertEqual(theirs.recv(64), helper.virtio_key_tap())
        self.assertTrue(any("pressed the VM's power key" in x for x in logs))
        self.assertIn("Force Off", ag.states[-1][1])
        theirs.send(b"\x11\x00\x00\x00\x00\x00\x00\x00")    # crosvm's LED status: drained, never blocks
        ag.drain()
        ours.close()
        theirs.close()

    def test_no_ssh_no_key_device(self):
        ag, logs = self.agent()
        self.assertFalse(ag.send("ping"))
        self.assertFalse(ag.send("poweroff"))
        self.assertIn("Force Off", logs[-1])
        self.assertIn("Force Off", ag.states[-1][1])             # the user is told at once (0.2-22)

    def test_stop_path_forces_at_once(self):
        ag, logs = self.agent(term=True)
        before = time.time()
        ag.power_key("the guest refused every ssh key")
        self.assertGreaterEqual(ag.sup.force_at, before)          # no 120 s grace on a refused shutdown


if __name__ == "__main__":
    unittest.main()
