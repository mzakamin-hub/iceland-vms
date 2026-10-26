"""Unit tests (PC) for parity step 3: the zink display mode (venus device, guest GL through zink)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from iceland_vms import config, crosvmargs  # noqa: E402

from test_core import RES, fedora_cfg  # noqa: E402


class ZinkMode(unittest.TestCase):
    def test_same_device_as_venus(self):
        v, z = fedora_cfg(), fedora_cfg()
        z["display"]["gpu"] = "zink"
        av, ev = crosvmargs.build(v, RES)
        az, ez = crosvmargs.build(z, RES)
        self.assertEqual(av, az)
        self.assertEqual(ev, ez)
        self.assertIn("--gpu-render-server", az)

    def test_needs_the_venus_feature(self):
        c = fedora_cfg()
        c["display"]["gpu"] = "zink"
        self.assertRaises(config.ConfigError, config.validate, c, {"gpu3d_venus": False})
        config.validate(c, {"gpu3d_venus": True})


if __name__ == "__main__":
    unittest.main()
