"""Unit tests (PC) for 0.2-10 step 4: the opt-in shared (pseudo-unprotected) memory mode.
Pure logic: no VM."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from iceland_vms import config, crosvmargs, host  # noqa: E402

from test_core import RES, fedora_cfg  # noqa: E402

FEATS = {"gpu3d_venus": True, "gpu3d_virgl": True, "simplefb": True, "gpu2d": True}


class MemoryMode(unittest.TestCase):
    def test_default_protected(self):
        self.assertEqual(config.default_config("a", "A")["memory_mode"], "protected")
        self.assertFalse(host.DEFAULT_FEATURES["memory_shared"])

    def test_old_file_filled(self):
        c = fedora_cfg()
        del c["memory_mode"]
        config.validate(c)
        self.assertEqual(c["memory_mode"], "protected")

    def test_bad_value(self):
        c = fedora_cfg()
        c["memory_mode"] = "open"
        self.assertRaises(config.ConfigError, config.validate, c)

    def test_protected_args(self):
        a, _ = crosvmargs.build(fedora_cfg(), RES)
        self.assertIn("--protected-vm-without-firmware", a)
        self.assertNotIn("--protected-vm-pseudo-unprotected", a)
        self.assertEqual(a[a.index("--swiotlb") + 1], "128")

    def test_shared_args(self):
        c = fedora_cfg()
        c["memory_mode"] = "shared"
        a, _ = crosvmargs.build(c, RES)
        self.assertIn("--protected-vm-pseudo-unprotected", a)
        self.assertNotIn("--protected-vm-without-firmware", a)
        self.assertNotIn("--swiotlb", a)

    def test_shared_needs_feature(self):
        c = fedora_cfg()
        c["memory_mode"] = "shared"
        self.assertRaises(config.ConfigError, config.validate, c, dict(FEATS))
        config.validate(c, dict(FEATS, memory_shared=True))

    def test_shared_not_on_qemu(self):
        c = fedora_cfg()
        c["memory_mode"] = "shared"
        self.assertIn("the shared (host-readable) memory mode", config.backend_conflicts(c, "qemu"))
        self.assertEqual(config.backend_conflicts(c, "crosvm"), [])

    def test_warning_says_host_can_read(self):
        self.assertIn("can read", config.MEMORY_SHARED_WARNING)


class VhostNet(unittest.TestCase):
    """vhost-net as a per-VM network setting: on by default since 0.2-15 (V11 gain on q90u)."""

    def test_default_on(self):
        self.assertTrue(config.default_config("a", "A")["network"]["vhost_net"])
        c = fedora_cfg()
        c["network"]["vhost_net"] = False
        a, _ = crosvmargs.build(c, RES)
        self.assertIn("tap-name=gvmtap0,mac=0a:1c:00:00:00:01", a)
        self.assertNotIn("vhost-net", " ".join(a))

    def test_on(self):
        c = fedora_cfg()
        c["network"]["vhost_net"] = True
        config.validate(c)
        a, _ = crosvmargs.build(c, RES)
        self.assertIn("tap-name=gvmtap0,mac=0a:1c:00:00:00:01,vhost-net", a)

    def test_bad_value(self):
        c = fedora_cfg()
        c["network"]["vhost_net"] = "yes"
        self.assertRaises(config.ConfigError, config.validate, c)


class ReleaseFixes(unittest.TestCase):
    """0.2-14: lease lookup by the VM's own MAC only; Windows VMs keep the image's MAC."""

    def test_lease_strict_mac(self):
        import tempfile
        from iceland_vms import gvmlib
        with tempfile.NamedTemporaryFile("w", delete=False) as f:
            f.write("1790000000 0a:1c:47:56:4d:02 10.250.0.85 * *\n")
            path = f.name
        old = gvmlib.LEASES
        try:
            gvmlib.LEASES = path
            self.assertIsNone(gvmlib.lease_ip("0a:1c:71:f1:61:73"))
            self.assertEqual(gvmlib.lease_ip("0A:1C:47:56:4D:02"), "10.250.0.85")
            self.assertEqual(gvmlib.lease_ip(), "10.250.0.85")
        finally:
            gvmlib.LEASES = old
            os.unlink(path)

    def test_windows_mac(self):
        c = config.windows_defaults(config.default_config("w", "W"))
        self.assertEqual(c["network"]["mac"], config.WINDOWS_IMAGE_MAC)


if __name__ == "__main__":
    unittest.main()
