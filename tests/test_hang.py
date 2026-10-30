"""Unit tests (PC) for 0.2-26: the window no longer waits for systemctl / rpm / modinfo on its main thread.
The VM states come from backend.StateMonitor (one systemctl per round, in a thread); host facts are cached."""
import os
import re
import subprocess
import sys
import threading
import time
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from iceland_vms import backend, config, host  # noqa: E402

UI = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "iceland_vms", "ui")


def listing(uid, rows):
    return "".join("%s loaded %s running x\n" % (config.unit_name(uid, v), a) for v, a in rows)


class Units(unittest.TestCase):
    def test_one_systemctl_for_every_vm(self):
        u = os.getuid()
        out = listing(u, [("aaa", "active"), ("bbb", "deactivating")]) + "iceland-vm-u99999-zzz.service loaded active running x\n"
        calls = []

        def run(argv, **kw):
            calls.append(argv)
            return types.SimpleNamespace(returncode=0, stdout=out, stderr="")
        with mock.patch.object(subprocess, "run", run):
            st, running = backend.states(["aaa", "bbb", "ccc"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][:2], ["systemctl", "list-units"])
        self.assertEqual(st["aaa"]["state"], "starting")          # active, no state.json yet
        self.assertEqual(st["bbb"]["state"], "stopping")
        self.assertEqual(st["ccc"]["state"], "stopped")           # no unit = inactive
        self.assertEqual(running, 2)                               # another user's VM counts for max_running

    def test_failure_is_none_not_stopped(self):
        def slow(argv, **kw):
            raise subprocess.TimeoutExpired(argv, kw.get("timeout"))
        with mock.patch.object(subprocess, "run", slow):
            self.assertIsNone(backend.vm_units())
            self.assertIsNone(backend.states(["aaa"]))
        with mock.patch.object(subprocess, "run", lambda a, **k: types.SimpleNamespace(returncode=1, stdout="", stderr="")):
            self.assertIsNone(backend.vm_units())

    def test_combine_matches_state(self):
        self.assertEqual(backend.combine("active", {"state": "running", "ip": "10.250.0.9", "agent": True}),
                         {"state": "running", "ip": "10.250.0.9", "message": "", "agent": True})
        self.assertEqual(backend.combine("inactive", {"state": "failed", "message": "x"})["state"], "failed")
        self.assertEqual(backend.combine("active", {"state": "stopped"})["state"], "stopping")


def st(state):
    return {"state": state, "ip": None, "message": "", "agent": False}


class Monitor(unittest.TestCase):
    def test_reports_changes_and_kicks_only(self):
        seq = [({"a": st("stopped")}, 0), ({"a": st("stopped")}, 0), ({"a": st("starting")}, 1), None]
        got = []
        m = backend.StateMonitor(lambda s, n: got.append((s, n)), collect=lambda ids: seq.pop(0))
        m.set_vms(["a"])
        m.poll_once()                       # forced (new list): reported
        m.poll_once()                       # unchanged: not reported
        m.poll_once()                       # changed: reported
        self.assertEqual(m.poll_once(), ({"a": st("starting")}, 1))     # failed round keeps the last answer
        self.assertEqual([g[0]["a"]["state"] for g in got], ["stopped", "starting"])

    def test_kick_never_waits_for_a_slow_systemd(self):
        gate = threading.Event()
        rounds = []

        def collect(ids):
            rounds.append(time.monotonic())
            gate.wait(5)                    # systemctl hanging (VM start, memory pressure)
            return {i: st("stopped") for i in ids}, 0
        m = backend.StateMonitor(lambda s, n: None, interval=0.05, busy_interval=0.05, collect=collect)
        m.start()
        try:
            time.sleep(0.1)
            t0 = time.monotonic()
            for _ in range(50):             # the UI asking repeatedly while systemctl hangs
                m.set_vms(["a", "b"])
                m.kick()
            self.assertLess(time.monotonic() - t0, 0.05)
            self.assertEqual(len(rounds), 1)            # never two rounds at once
        finally:
            gate.set()
            m.stop()

    def test_faster_while_a_vm_changes_state(self):
        busy = {"n": 0}
        lock = threading.Lock()

        def collect(ids):
            with lock:
                busy["n"] += 1
            return {"a": st("starting")}, 1
        m = backend.StateMonitor(lambda s, n: None, interval=5, busy_interval=0.02, collect=collect)
        m.start()
        time.sleep(0.3)
        m.stop()
        self.assertGreater(busy["n"], 3)


class HostFacts(unittest.TestCase):
    def test_modinfo_and_rpm_once(self):
        host.forget()
        n = {"c": 0}

        def run(argv, **kw):
            n["c"] += 1
            return types.SimpleNamespace(returncode=0, stdout="0.3-59", stderr="")
        with mock.patch.object(subprocess, "run", run):
            for _ in range(5):
                self.assertTrue(host.modules_match())
                self.assertEqual(host.gvm_version(), "0.3-59")
            self.assertEqual(n["c"], 2)
            host.forget()
            host.modules_match()
            self.assertEqual(n["c"], 3)
        host.forget()


class FakeModel:
    def __init__(self, items, row):
        self.items, self.row = items, row

    def get_string(self, i):
        return self.items[i]

    def splice(self, i, n, new):
        self.items[i:i + n] = new
        if self.row.selected == i:          # GTK: replacing the selected item re-selects -> notify::selected
            self.row.on_selected()


class FakeRow:
    def __init__(self, items, selected, on_selected):
        self.model = FakeModel(list(items), self)
        self.selected, self.on_selected = selected, on_selected

    def get_model(self):
        return self.model

    def get_selected(self):
        return self.selected

    def set_selected(self, i):
        self.selected = i


class Relabel(unittest.TestCase):
    """Settings of a VM with the automatic backend spun in update_conflict -> splice -> notify::selected."""

    def setUp(self):
        try:
            from iceland_vms.ui import util
        except (ImportError, ValueError) as e:      # no PyGObject / GTK 4 on this PC
            self.skipTest(str(e))
        self.util = util

    def test_selected_item_relabel_terminates(self):
        calls = {"n": 0}

        def handler():                      # what update_conflict does: relabel item 0 again
            calls["n"] += 1
            if calls["n"] > 50:
                raise RuntimeError("endless notify::selected loop")
            self.util.set_label(row, 0, "Automatic (QEMU-Gunyah)" if calls["n"] % 2 else "Automatic (crosvm)")
        row = FakeRow(["Automatic (crosvm)", "crosvm", "QEMU-Gunyah"], 0, handler)
        self.util.set_label(row, 0, "Automatic (QEMU-Gunyah)")
        self.assertLessEqual(calls["n"], 1)
        self.assertEqual(row.selected, 0)

    def test_same_text_is_a_noop(self):
        row = FakeRow(["Automatic (crosvm)", "crosvm"], 0, lambda: self.fail("spliced although unchanged"))
        self.util.set_label(row, 0, "Automatic (crosvm)")

    def test_real_string_list(self):
        from gi.repository import Gtk
        sl = Gtk.StringList.new(["Automatic (crosvm)", "crosvm", "QEMU-Gunyah"])
        sel = Gtk.SingleSelection(model=sl)
        n = {"c": 0}

        class Row:
            def get_model(self):
                return sl

            def get_selected(self):
                return sel.get_selected()

            def set_selected(self, i):
                sel.set_selected(i)
        row = Row()

        def notify(*_):
            n["c"] += 1
            if n["c"] > 50:
                raise RuntimeError("loop")
            self.util.set_label(row, 0, "Automatic (QEMU-Gunyah)")
        sel.connect("notify::selected", notify)
        self.util.set_label(row, 0, "Automatic (QEMU-Gunyah)")
        self.assertEqual(sl.get_string(0), "Automatic (QEMU-Gunyah)")
        self.assertEqual(sel.get_selected(), 0)
        self.assertLessEqual(n["c"], 3)


class MallocArenas(unittest.TestCase):
    def test_env_wins(self):
        import iceland_vms
        with mock.patch.dict(os.environ, {"MALLOC_ARENA_MAX": "4"}):
            self.assertFalse(iceland_vms.limit_malloc_arenas())

    def test_sets_limit_in_a_child(self):
        # a fresh interpreter: the limit is accepted by glibc (mallopt returns 1)
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        env = {k: v for k, v in os.environ.items() if k != "MALLOC_ARENA_MAX"}
        r = subprocess.run([sys.executable, "-c", "import iceland_vms; print(iceland_vms.limit_malloc_arenas())"],
                           cwd=root, env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(r.stdout.strip(), "True", r.stderr)

    def test_vmm_env(self):
        from iceland_vms import helper
        e = helper.vmm_env({"PATH": "/usr/bin"}, {"CROSVM_GVM_ACCEPT": "1"}, {"name": "Win"})
        self.assertEqual(e["MALLOC_ARENA_MAX"], "4")
        self.assertEqual(e["CROSVM_GVM_ACCEPT"], "1")
        self.assertEqual(e["CROSVM_GVM_WINDOW_TITLE"], "Win - Virtual Machines")
        self.assertEqual(helper.vmm_env({"MALLOC_ARENA_MAX": "8"}, {}, {"name": "x"})["MALLOC_ARENA_MAX"], "8")

    def test_launchers_call_it_before_the_ui(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for f in ("bin/iceland-vms", "iceland_vms/__main__.py"):
            with open(os.path.join(root, f), encoding="utf-8") as fh:
                src = fh.read()
            self.assertLess(src.index("limit_malloc_arenas()"), src.index("ui.app import main"), f)


class NoBlockingInTheWindow(unittest.TestCase):
    """The GTK modules must not call the blocking state / host helpers directly (they run on the main loop)."""

    def test_ui_sources(self):
        bad = re.compile(r"backend\.(state|running_count|unit_state|vm_units|states)\(|subprocess\.|time\.sleep\(")
        hits = []
        for n in sorted(os.listdir(UI)):
            if n.endswith(".py") and n != "shots.py":
                with open(os.path.join(UI, n), encoding="utf-8") as f:
                    for i, line in enumerate(f, 1):
                        if bad.search(line):
                            hits.append("%s:%d: %s" % (n, i, line.strip()))
        self.assertEqual(hits, [])

    def test_slow_host_calls_only_in_threads(self):
        # modules_match / gvm_version may appear only inside a function handed to util.in_thread
        for n in ("app.py", "hostinfo.py"):
            with open(os.path.join(UI, n), encoding="utf-8") as f:
                src = f.read()
            for call in ("host.modules_match()", "host.gvm_version()"):
                for m in re.finditer(re.escape(call), src):
                    before = src[:m.start()]
                    self.assertIn("def facts():", before[before.rfind("    def "):],
                                  "%s: %s outside a thread" % (n, call))


if __name__ == "__main__":
    unittest.main()
