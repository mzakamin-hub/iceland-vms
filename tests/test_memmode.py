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
        self.assertTrue(host.DEFAULT_FEATURES["memory_shared"])      # unlocked in 0.2-18; still opt-in per VM

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
        c["display"]["gpu"] = "simplefb"           # 0.2-19: no 3D display in the shared mode
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


class Shared3D(unittest.TestCase):
    """0.2-19 refused every 3D display with the shared memory mode; 0.2-20 allows 3D OpenGL (virgl, gvm 0.3-57
    PV-B-3: glmark2 1634) behind the feature memory_shared_3d, Vulkan / native stay refused."""

    def test_vulkan_native_refused(self):
        for gpu in config.SHARED_3D_BLOCKED:
            c = fedora_cfg()
            c["memory_mode"] = "shared"
            c["display"]["gpu"] = gpu
            self.assertRaises(config.ConfigError, config.validate, c)
        self.assertEqual(set(config.SHARED_3D_BLOCKED), {"native", "venus", "zink"})

    def test_virgl_allowed_with_feature(self):
        c = fedora_cfg()
        c["memory_mode"] = "shared"
        c["display"]["gpu"] = "virgl"
        config.validate(c)
        config.validate(c, dict(FEATS, memory_shared=True, memory_shared_3d=True))
        a, env = crosvmargs.build(c, RES)
        self.assertIn("--protected-vm-pseudo-unprotected", a)
        self.assertNotIn("--swiotlb", a)
        self.assertTrue(any("context-types=virgl:virgl2," in x for x in a))

    def test_virgl_refused_without_feature(self):
        c = fedora_cfg()
        c["memory_mode"] = "shared"
        c["display"]["gpu"] = "virgl"
        with self.assertRaises(config.ConfigError) as cm:
            config.validate(c, dict(FEATS, memory_shared=True, memory_shared_3d=False))
        self.assertIn("memory_shared_3d", str(cm.exception))

    def test_default_feature_on(self):
        self.assertTrue(host.DEFAULT_FEATURES["memory_shared_3d"])

    def test_software_display_ok(self):
        for gpu in ("simplefb", "none", "2d"):
            c = fedora_cfg()
            c["memory_mode"] = "shared"
            c["display"]["gpu"] = gpu
            config.validate(c, dict(FEATS, memory_shared=True, memory_shared_3d=False))

    def _load(self, gpu):
        import json
        import tempfile
        c = fedora_cfg()
        c["memory_mode"] = "shared"
        c["display"]["gpu"] = gpu
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "vm.json")
            with open(p, "w") as f:
                json.dump(c, f)
            return config.load(p)

    def test_saved_shared_venus_loads_as_software_display(self):
        got = self._load("venus")
        self.assertEqual(got["display"]["gpu"], "simplefb")
        config.validate(got)

    def test_saved_shared_virgl_kept(self):
        got = self._load("virgl")
        self.assertEqual(got["display"]["gpu"], "virgl")
        config.validate(got)

    def test_protected_3d_ok(self):
        for gpu in ("virgl", "venus", "zink"):
            c = fedora_cfg()
            c["display"]["gpu"] = gpu
            config.validate(c)


if __name__ == "__main__":
    unittest.main()
