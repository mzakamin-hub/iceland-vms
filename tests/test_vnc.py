"""Unit tests (PC) for 0.2-7: the VNC display option (config, crosvm / QEMU arguments, viewer launcher).
Pure logic: no VM, no VNC server."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from iceland_vms import config, crosvmargs, qemuargs  # noqa: E402

from test_core import RES, fedora_cfg  # noqa: E402

FEATS = {"gpu3d_venus": True, "gpu3d_virgl": True, "simplefb": True, "gpu2d": True}


def vnc_cfg(**kw):
    c = fedora_cfg()
    c["display"].update(vnc=True, **kw)
    return c


class VncConfig(unittest.TestCase):
    def test_defaults_off(self):
        d = config.default_config("a", "A")["display"]
        self.assertEqual((d["vnc"], d["vnc_lan"], d["vnc_port"], d["vnc_password"]), (False, False, 5900, False))

    def test_lan_needs_password(self):
        c = vnc_cfg(vnc_lan=True)
        self.assertRaises(config.ConfigError, config.validate, c, FEATS)
        c["display"]["vnc_password"] = True
        config.validate(c, FEATS)

    def test_port_range_and_display(self):
        self.assertRaises(config.ConfigError, config.validate, vnc_cfg(vnc_port=5800), FEATS)
        c = vnc_cfg()
        c["display"]["gpu"] = "none"
        self.assertRaises(config.ConfigError, config.validate, c, FEATS)

    def test_qemu_lan_refused(self):
        c = vnc_cfg(vnc_lan=True, vnc_password=True)
        c["backend"] = "qemu"
        self.assertRaises(config.ConfigError, config.validate, c, FEATS)


class VncArgs(unittest.TestCase):
    def test_crosvm_local_no_window(self):
        a, _env = crosvmargs.build(vnc_cfg(), RES)
        i = a.index("--vnc-server")
        self.assertEqual(a[i + 1], "host=127.0.0.1,port=5900")
        self.assertNotIn("--wayland-sock", a)
        self.assertIn("--display-window-mouse", a)

    def test_crosvm_lan_password(self):
        res = dict(RES, vnc_password_file="/proc/self/fd/9")
        a, _ = crosvmargs.build(vnc_cfg(vnc_lan=True, vnc_password=True, vnc_port=5905), res)
        self.assertEqual(a[a.index("--vnc-server") + 1], "host=0.0.0.0,port=5905,password-file=/proc/self/fd/9")

    def test_crosvm_simplefb(self):
        c = vnc_cfg()
        c["display"]["gpu"] = "simplefb"
        a, _ = crosvmargs.build(c, RES)
        self.assertIn("--simplefb", a)
        self.assertEqual(a[a.index("--vnc-server") + 1], "host=127.0.0.1,port=5900")
        self.assertNotIn("--wayland-sock", a)

    def test_no_vnc_keeps_window(self):
        a, _ = crosvmargs.build(fedora_cfg(), RES)
        self.assertNotIn("--vnc-server", a)
        self.assertIn("--wayland-sock", a)

    def test_qemu_vnc(self):
        c = vnc_cfg(vnc_port=5902)
        c["backend"] = "qemu"
        a, _ = qemuargs.build(c, dict(RES, qemu_firmware="/x.fd"))
        self.assertIn("-vnc", a)
        self.assertEqual(a[a.index("-vnc") + 1], "127.0.0.1:2")
        self.assertNotIn("gtk,gl=off,zoom-to-fit=on", a)

    def test_qemu_gl_only_with_feature(self):
        c = fedora_cfg()
        c["backend"] = "qemu"
        a, _ = qemuargs.build(c, dict(RES, qemu_firmware="/x.fd"))
        self.assertTrue(any(x.startswith("virtio-gpu-pci,") for x in a))
        # 0.2-9: guest RAM in a shared memfd (the swiotlb faults of anonymous RAM, p3 QG)
        self.assertIn("memory-backend-memfd,id=gram,size=%dM,share=on" % (c["memory_mib"] + c["swiotlb_mib"]), a)
        self.assertIn("virt,confidential-guest-support=cg0,memory-backend=gram", a)
        a, _ = qemuargs.build(c, dict(RES, qemu_firmware="/x.fd"), {"qemu_gl": True})
        self.assertIn("virtio-gpu-gl-pci,xres=1600,yres=1000,blob=false", a)
        self.assertIn("gtk,gl=on,zoom-to-fit=on,show-menubar=off", a)
        c["display"]["vnc"] = True
        a, _ = qemuargs.build(c, dict(RES, qemu_firmware="/x.fd"), {"qemu_gl": True})
        self.assertEqual(a[a.index("-display") + 1], "egl-headless")
        self.assertEqual(a[a.index("-vnc") + 1], "127.0.0.1:0")

    def test_viewer_argv(self):
        c = vnc_cfg(vnc_port=5901)
        self.assertEqual(config.vnc_viewer_argv(c, "krdc"), ["krdc", "vnc://127.0.0.1:5901"])
        self.assertEqual(config.vnc_viewer_argv(c, "vncviewer"), ["vncviewer", "127.0.0.1::5901"])
        self.assertIsNone(config.vnc_viewer_argv(fedora_cfg(), "krdc"))


if __name__ == "__main__":
    unittest.main()
