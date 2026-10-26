"""Unit tests (PC) for 0.2-22 keyboard grab (display.keyboard_grab -> CROSVM_GVM_KBD_GRAB, iceland-gvm 0.3-58)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from iceland_vms import config, crosvmargs  # noqa: E402

from test_core import RES, fedora_cfg  # noqa: E402


class KeyboardGrab(unittest.TestCase):
    def test_default_off(self):
        self.assertFalse(config.default_config("a", "A")["display"]["keyboard_grab"])
        _, env = crosvmargs.build(fedora_cfg(), RES)
        self.assertNotIn("CROSVM_GVM_KBD_GRAB", env)

    def test_on_in_the_window(self):
        c = fedora_cfg()
        c["display"]["keyboard_grab"] = True
        config.validate(c)
        _, env = crosvmargs.build(c, dict(RES, wayland_sock="/run/user/1000/wayland-0"))
        self.assertEqual(env.get("CROSVM_GVM_KBD_GRAB"), "1")

    def test_not_without_window_or_keyboard(self):
        c = fedora_cfg()
        c["display"]["keyboard_grab"] = True
        c["display"]["vnc"] = True                                  # headless: nothing to grab
        _, env = crosvmargs.build(c, dict(RES, wayland_sock="/run/user/1000/wayland-0"))
        self.assertNotIn("CROSVM_GVM_KBD_GRAB", env)
        c["display"]["vnc"] = False
        c["display"]["keyboard"] = False
        _, env = crosvmargs.build(c, dict(RES, wayland_sock="/run/user/1000/wayland-0"))
        self.assertNotIn("CROSVM_GVM_KBD_GRAB", env)

    def test_validated_and_optional(self):
        c = fedora_cfg()
        c["display"]["keyboard_grab"] = "yes"
        self.assertRaises(config.ConfigError, config.validate, c)
        del c["display"]["keyboard_grab"]                           # a 0.2-21 vm.json
        config.validate(c)


if __name__ == "__main__":
    unittest.main()
