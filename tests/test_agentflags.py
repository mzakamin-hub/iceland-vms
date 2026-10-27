"""0.2-24: the guest agent finds the app's flags (the app shares its tools dir itself), existing VMs get autologin
once, new Fedora 44+ VMs default to the native GPU, the app refreshes an older guest agent over ssh."""
import json
import os
import re
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from iceland_vms import config, helper  # noqa: E402

AGENT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "guest-tools", "gvm-agent")


class AgentFlags(unittest.TestCase):
    def setUp(self):
        with open(AGENT) as f:
            self.s = f.read()

    def test_both_share_layouts(self):
        self.assertIn('ft=$share/tools; [ -d "$ft" ] || ft=$share', self.s)
        for flag in ("noglamor", "zink", "autologin", "wayland", "native"):
            self.assertIn('[ -f "$ft/%s" ]' % flag, self.s)
        # no flag is read from tools/ only any more (only the agent lookup names both places)
        left = [ln for ln in self.s.splitlines() if "$share/tools/" in ln and "gvm-agent" not in ln]
        self.assertEqual(left, [])

    def test_flags_marker_and_refresh(self):
        self.assertIn("/run/gvm-agent.flags", self.s)
        self.assertIn('[ "${1:-}" = --refresh ]', self.s)
        self.assertIn("systemctl restart display-manager.service", self.s)

    def test_no_xserver_command(self):
        """0.2-25 (kit-vm24b B-4 / B-5 / N-4 / N-5): lightdm started no X server with xserver-command=env ...; zink
        goes through Mesa's driconf, the native Mesa through ld.so.conf.d; the 0.2-24 files are removed."""
        code = "\n".join(ln for ln in self.s.splitlines() if not ln.lstrip().startswith("#"))
        self.assertNotIn("xserver-command", code)
        self.assertNotIn("LD_LIBRARY_PATH", code)
        self.assertIn('<option name="dri_driver" value="zink" />', self.s)
        self.assertIn('kernel_driver="virtio_gpu"', self.s)
        self.assertIn("/etc/ld.so.conf.d/iceland-nctx.conf", self.s)
        for old in ("51-iceland-zink.conf", "52-iceland-nctx.conf", "/etc/profile.d/iceland-nctx.sh",
                    "/etc/environment.d/60-iceland-nctx.conf"):
            self.assertIn(old, code)            # still named: removed on every setup

    def test_dmcheck_reboots_once_on_fresh_boots_only(self):
        self.assertIn('[ "${1:-}" = --dmcheck ]', self.s)
        self.assertIn("--on-active=25 /usr/local/sbin/gvm-agent --dmcheck", self.s)
        blk = self.s[self.s.index('[ "${1:-}" = --dmcheck ]'):self.s.index("dev=/dev/hvc0")]
        self.assertIn('[ "$up" -lt 600 ]', blk)
        self.assertIn('[ "$(cat "$st" 2>/dev/null)" != "$k" ]', blk)
        self.assertLess(blk.index('echo "$k" > "$st"'), blk.index("systemctl reboot"))

    def test_refresh_command(self):
        c = helper.AGENT_REFRESH_CMD
        self.assertTrue(c.startswith("sudo -n sh -c '") and c.endswith("'") and c.count("'") == 2)
        for k in ("AGENT-CURRENT", "AGENT-UPDATED", "AGENT-NOSHARE", "--refresh", "/mnt/gvmshare/gvm-agent"):
            self.assertIn(k, c)


class Defaults(unittest.TestCase):
    def test_new_vm_gpu(self):
        old = config.GUEST_MESA_DIR
        with tempfile.TemporaryDirectory() as d:
            open(os.path.join(d, "nctx-mesa-26.2.5.tar.gz"), "w").close()
            try:
                config.GUEST_MESA_DIR = d
                on = {"gpu3d_native": True}
                self.assertEqual(config.new_vm_gpu("fedora-44-cloud", on), "native")
                self.assertEqual(config.new_vm_gpu("fedora-45-cloud", on), "native")
                self.assertEqual(config.new_vm_gpu("fedora-43-cloud", on), "venus")
                self.assertEqual(config.new_vm_gpu("ubuntu-26.04-cloud", on), "venus")
                self.assertEqual(config.new_vm_gpu("", on), "venus")
                self.assertEqual(config.new_vm_gpu("fedora-44-cloud", {"gpu3d_native": False}), "venus")
                self.assertEqual(config.new_vm_gpu("fedora-44-cloud", on, windows=True), "simplefb")
                config.GUEST_MESA_DIR = "/nonexistent"
                self.assertEqual(config.new_vm_gpu("fedora-44-cloud", on), "venus")
            finally:
                config.GUEST_MESA_DIR = old

    def test_shipped_features_enable_native(self):
        p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "features.json")
        with open(p) as f:
            self.assertTrue(json.load(f)["gpu3d_native"])

    def _roundtrip(self, mutate):
        c = config.default_config("t1", "T")
        mutate(c)
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "vm.json")
            with open(p, "w") as f:
                json.dump(c, f)
            return config.load(p)

    def test_existing_vm_gets_autologin_once(self):
        def old(c):
            c["display"].update(autologin=False, defaults_rev=2, gpu="virgl", glamor=False, session="wayland")
        got = self._roundtrip(old)
        self.assertTrue(got["display"]["autologin"])
        self.assertEqual(got["display"]["defaults_rev"], 3)
        # the rev-2 migration (glamor / session) is not repeated; the display mode is kept
        self.assertFalse(got["display"]["glamor"])
        self.assertEqual(got["display"]["session"], "wayland")
        self.assertEqual(got["display"]["gpu"], "virgl")

    def test_rev3_choice_is_kept(self):
        got = self._roundtrip(lambda c: c["display"].update(autologin=False, defaults_rev=3))
        self.assertFalse(got["display"]["autologin"])

    def test_new_config_is_rev3(self):
        self.assertEqual(config.default_config("t2", "T")["display"]["defaults_rev"], 3)


if __name__ == "__main__":
    unittest.main()
