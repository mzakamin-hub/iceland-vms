"""Unit tests (PC) for 0.2-6: the fullscreen size rule and the VIN judge / calibration (gvm-touchinject.py) on
synthetic recorder output. Pure logic: no uinput device is created, no VM runs."""
import importlib.util
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from iceland_vms import config  # noqa: E402

# gvm-touchinject.py lives in the iceland-gvm source (gvm/); in the iceland-vms repo alone the judge tests skip
_TI = next((p for p in (os.path.join(HERE, "gvm03", "gvm-touchinject.py"), os.path.join(HERE, "gvm", "gvm-touchinject.py"),
                        "/usr/libexec/iceland-gvm/gvm-touchinject.py") if os.path.exists(p)), None)
if _TI:
    _spec = importlib.util.spec_from_file_location("touchinject", _TI)
    ti = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(ti)
else:
    ti = None

TAB, MOU, TCH = "gvm window tablet", "Crosvm Virtio Mouse 4294967290", "Crosvm Virtio Multitouch Touchscreen 4294967295"
W, H = 1600, 1000


def rec(devs, evs):
    f = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False)
    for d in devs:
        f.write(json.dumps({"dev": d, "path": "/dev/input/eventX"}) + "\n")
    f.write("READY %d devices\n" % len(devs))
    t = 1.0
    for d, ty, c, v, *dt in evs:
        t += dt[0] if dt else 0.01
        f.write(json.dumps({"d": d, "t": t, "ty": ty, "c": c, "v": v}) + "\n")
    f.write("END\n")
    f.close()
    return f.name


def ev_click(dev, code, x=None, y=None, held=0.05):
    e = []
    if x is not None:
        e += [(dev, ti.EV_ABS, ti.ABS_X, x), (dev, ti.EV_ABS, ti.ABS_Y, y)]
    return e + [(dev, ti.EV_KEY, code, 1), (dev, ti.EV_KEY, code, 0, held)]


class FullscreenSize(unittest.TestCase):
    def cfg(self, **kw):
        c = config.default_config("a", "A")
        c["display"].update(kw)
        return c

    def test_not_fullscreen_or_off(self):
        self.assertIsNone(config.fullscreen_size(self.cfg(), [("DSI-1", 3392, 2400)]))
        self.assertIsNone(config.fullscreen_size(self.cfg(fullscreen=True, fullscreen_native=False), [("DSI-1", 3392, 2400)]))
        self.assertIsNone(config.fullscreen_size(self.cfg(fullscreen=True), []))

    def test_named_output_first_and_clamped(self):
        mons = [("DSI-1", 3392, 2400), ("DP-1", 1920, 1080)]
        self.assertEqual(config.fullscreen_size(self.cfg(fullscreen=True, output="DP-1"), mons), (1920, 1080))
        self.assertEqual(config.fullscreen_size(self.cfg(fullscreen=True, output="HDMI-9"), mons), (3392, 2400))
        self.assertEqual(config.fullscreen_size(self.cfg(fullscreen=True), [("X", 7680, 4320)]), (3840, 3000))
        c = self.cfg(fullscreen=True)
        c["boot"]["root"] = "/dev/vda2"
        c["display"]["gpu"] = "none"
        c["display"]["width"], c["display"]["height"] = config.fullscreen_size(c, mons)
        config.validate(c, {})

    def test_validate_bool(self):
        c = self.cfg(fullscreen_native="yes")
        self.assertRaises(config.ConfigError, config.validate, c, {})


@unittest.skipIf(ti is None, "gvm-touchinject.py (iceland-gvm) not available")
class Judge(unittest.TestCase):
    def j(self, mode, g, devs, evs, ex=None):
        p = rec(devs, evs)
        try:
            return ti.judge(mode, g, W, H, p, ex)
        finally:
            os.unlink(p)

    def test_devices(self):
        ok, t = self.j("tablet", "devices", [TCH, TAB, MOU], [])
        self.assertTrue(ok, t)
        ok, t = self.j("touch", "devices", [TCH, TAB, MOU], [])
        self.assertFalse(ok, t)
        self.assertTrue(self.j("touch", "devices", [TCH], [])[0])

    def test_tablet_tap_lands(self):
        ok, t = self.j("tablet", "tap", [TCH, TAB, MOU], ev_click(TAB, ti.BTN_LEFT, 800, 500), (0.5, 0.5))
        self.assertTrue(ok, t)
        ok, t = self.j("tablet", "tap", [TCH, TAB, MOU], ev_click(TAB, ti.BTN_LEFT, 400, 500), (0.5, 0.5))
        self.assertFalse(ok, t)
        self.assertIn("at (0.250,0.500)", t)

    def test_tablet_tap_on_touchscreen_fails(self):
        evs = [(TCH, ti.EV_ABS, ti.ABS_MT_TRACKING_ID, 1)] + ev_click(TAB, ti.BTN_LEFT, 800, 500)
        self.assertFalse(self.j("tablet", "tap", [TCH, TAB], evs, (0.5, 0.5))[0])

    def test_tablet_press_and_two_finger_tap_are_right_clicks(self):
        self.assertTrue(self.j("tablet", "press", [TAB], ev_click(TAB, ti.BTN_RIGHT, 800, 500), (0.5, 0.5))[0])
        self.assertFalse(self.j("tablet", "press", [TAB], ev_click(TAB, ti.BTN_LEFT, 800, 500), (0.5, 0.5))[0])
        self.assertTrue(self.j("tablet", "tap2", [TAB], ev_click(TAB, ti.BTN_RIGHT, 800, 500))[0])

    def test_tablet_drag(self):
        evs = [(TAB, ti.EV_ABS, ti.ABS_X, 640), (TAB, ti.EV_ABS, ti.ABS_Y, 500), (TAB, ti.EV_KEY, ti.BTN_LEFT, 1)]
        evs += [(TAB, ti.EV_ABS, ti.ABS_X, 640 + 16 * i) for i in range(1, 21)]
        evs += [(TAB, ti.EV_KEY, ti.BTN_LEFT, 0)]
        ok, t = self.j("tablet", "drag", [TAB], evs)
        self.assertTrue(ok, t)
        self.assertFalse(self.j("tablet", "drag", [TAB], ev_click(TAB, ti.BTN_LEFT, 800, 500))[0])

    def test_tablet_scroll(self):
        evs = [(TAB, ti.EV_REL, ti.REL_WHEEL, -1)] * 4
        self.assertTrue(self.j("tablet", "scroll2", [TAB], evs)[0])
        evs = [(TAB, ti.EV_REL, ti.REL_HWHEEL, 1)] * 3      # a rotated output scrolls sideways: still counted
        self.assertTrue(self.j("tablet", "scroll2", [TAB], evs)[0])
        self.assertFalse(self.j("tablet", "scroll2", [TAB], evs + ev_click(TAB, ti.BTN_RIGHT))[0])

    def test_touchpad(self):
        self.assertTrue(self.j("touchpad", "tap", [MOU], ev_click(MOU, ti.BTN_LEFT))[0])
        self.assertTrue(self.j("touchpad", "press", [MOU], ev_click(MOU, ti.BTN_RIGHT))[0])
        self.assertFalse(self.j("touchpad", "tap", [MOU, TAB], ev_click(TAB, ti.BTN_LEFT, 1, 1))[0])
        evs = [(MOU, ti.EV_REL, ti.REL_X, 10)] * 5
        self.assertTrue(self.j("touchpad", "drag", [MOU], evs)[0])
        self.assertTrue(self.j("touchpad", "scroll2", [MOU], [(MOU, ti.EV_REL, ti.REL_WHEEL, 1)] * 3)[0])

    def test_touch(self):
        down = [(TCH, ti.EV_ABS, ti.ABS_MT_SLOT, 0), (TCH, ti.EV_ABS, ti.ABS_MT_TRACKING_ID, 3),
                (TCH, ti.EV_ABS, ti.ABS_MT_POSITION_X, 800), (TCH, ti.EV_ABS, ti.ABS_MT_POSITION_Y, 500),
                (TCH, ti.EV_KEY, ti.BTN_TOUCH, 1)]
        up = [(TCH, ti.EV_ABS, ti.ABS_MT_TRACKING_ID, -1), (TCH, ti.EV_KEY, ti.BTN_TOUCH, 0)]
        self.assertTrue(self.j("touch", "tap", [TCH], down + up, (0.5, 0.5))[0])
        longup = [(TCH, ti.EV_ABS, ti.ABS_MT_TRACKING_ID, -1, 1.0), (TCH, ti.EV_KEY, ti.BTN_TOUCH, 0)]
        self.assertTrue(self.j("touch", "press", [TCH], down + longup, (0.5, 0.5))[0])
        self.assertFalse(self.j("touch", "press", [TCH], down + up, (0.5, 0.5))[0])
        two = down + [(TCH, ti.EV_ABS, ti.ABS_MT_SLOT, 1), (TCH, ti.EV_ABS, ti.ABS_MT_TRACKING_ID, 4)] + \
            [(TCH, ti.EV_ABS, ti.ABS_MT_TRACKING_ID, -1), (TCH, ti.EV_ABS, ti.ABS_MT_SLOT, 0)] + up
        self.assertTrue(self.j("touch", "tap2", [TCH], two)[0])
        self.assertFalse(self.j("touch", "tap2", [TCH], down + up)[0])
        drag = down + [(TCH, ti.EV_ABS, ti.ABS_MT_POSITION_X, 800 + 20 * i) for i in range(10)] + up
        self.assertTrue(self.j("touch", "drag", [TCH], drag)[0])

    def test_press_at_the_same_point_uses_the_initial_axes(self):
        # p3: evdev sends no ABS_X / ABS_Y when they equal the last values; the recorder's init lines carry them
        f = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False)
        f.write(json.dumps({"dev": TAB, "path": "/dev/input/event2"}) + "\n")
        f.write(json.dumps({"init": TAB, "c": ti.ABS_X, "v": 640}) + "\n")
        f.write(json.dumps({"init": TAB, "c": ti.ABS_Y, "v": 600}) + "\n")
        f.write("READY 1 devices\n")
        for v in (1, 0):
            f.write(json.dumps({"d": TAB, "t": 1.0 + v, "ty": ti.EV_KEY, "c": ti.BTN_RIGHT, "v": v}) + "\n")
        f.close()
        try:
            ok, t = ti.judge("tablet", "press", W, H, f.name, (0.4, 0.6))
        finally:
            os.unlink(f.name)
        self.assertTrue(ok, t)

    def test_init_lines_alone_are_no_events(self):
        f = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False)
        f.write(json.dumps({"dev": TAB, "path": "/dev/input/event2"}) + "\n")
        f.write(json.dumps({"init": TAB, "c": ti.ABS_X, "v": 640}) + "\n")
        f.close()
        try:
            self.assertFalse(ti.judge("tablet", "tap", W, H, f.name)[0])
        finally:
            os.unlink(f.name)

    def test_no_events(self):
        self.assertFalse(self.j("tablet", "tap", [TAB], [])[0])


@unittest.skipIf(ti is None, "gvm-touchinject.py (iceland-gvm) not available")
class Calibration(unittest.TestCase):
    def check(self, rot, f):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "fit.json")
            pts = [(x, y) + f(x, y) for x, y in ((0.3, 0.3), (0.7, 0.3), (0.3, 0.7))]
            _m, _c, name = ti.fit(p, pts)
            self.assertEqual(name, rot)
            for sx, sy in ((0.5, 0.5), (0.2, 0.8), (0.9, 0.1)):
                dx, dy = ti.inv(p, sx, sy)
                self.assertAlmostEqual(f(dx, dy)[0], sx, places=6)
                self.assertAlmostEqual(f(dx, dy)[1], sy, places=6)

    def test_rotations(self):
        self.check("normal", lambda x, y: (x, y))
        self.check("rot90", lambda x, y: (1 - y, x))
        self.check("rot180", lambda x, y: (1 - x, 1 - y))
        self.check("rot270", lambda x, y: (y, 1 - x))

    def test_transpose_and_four_points(self):
        # p1 (q90r): device (0.7, 0.3) landed at (0.3, 0.7): the tablet's output maps the injector transposed
        f = lambda x, y: (y, x)  # noqa: E731
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "fit.json")
            pts = [(x, y) + f(x, y) for x, y in ((0.3, 0.3), (0.7, 0.3), (0.3, 0.7), (0.7, 0.7))]
            self.assertEqual(ti.fit(p, pts)[2], "transpose")
            dx, dy = ti.inv(p, 0.4, 0.6)
            self.assertAlmostEqual(dx, 0.6, places=6)
            self.assertAlmostEqual(dy, 0.4, places=6)

    def test_three_points_without_the_corner(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "fit.json")
            pts = [(0.7, 0.3, 0.3, 0.7), (0.3, 0.7, 0.7, 0.3), (0.7, 0.7, 0.7, 0.7)]
            self.assertEqual(ti.fit(p, pts)[2], "transpose")

    def test_no_fit_is_identity(self):
        self.assertEqual(ti.inv("/nonexistent/fit.json", 0.25, 0.75), (0.25, 0.75))


if __name__ == "__main__":
    unittest.main()
