"""Host status: what the tablet has for VMs, what is proven, and the cleanup actions."""
from gi.repository import Adw, Gtk

from .. import backend, config, gvmlib, host
from . import util

FEATURE_LABEL = {
    "direct_kernel": "Direct kernel boot (V1 / V1b)", "network_nat": "NAT network (V2)", "gpu2d": "2D display window (V3)",
    "uefi": "UEFI firmware boot (ISOs)", "gpu3d_virgl": "3D virgl (V4)", "gpu3d_native": "3D msm native context (V4)",
    "gpu3d_venus": "3D venus (V4)", "sound": "Sound", "qemu_backend": "QEMU-Gunyah backend",
}


class HostDialog(Adw.Dialog):
    def __init__(self, win):
        super().__init__(title="Host Status", content_width=560, content_height=680)
        self.win = win
        tv = Adw.ToolbarView()
        tv.add_top_bar(Adw.HeaderBar())
        pp = Adw.PreferencesPage()
        tv.set_content(pp)
        self.set_child(tv)

        g = Adw.PreferencesGroup(title="Tablet")
        cap, on, status = host.power()
        m = host.meminfo()
        t = host.max_temp_c()
        sets = host.cpu_sets()
        for k, v in (("Kernel", host.kernel()),
                     ("Battery", "%s (%s)" % ("%d %%" % cap if cap is not None else "none", "charger" if on else status)),
                     ("Memory", "%s MiB free of %s MiB; up to %d MiB for a guest" % (m.get("MemAvailable", "?"),
                                                                                 m.get("MemTotal", "?"), host.max_guest_mib())),
                     ("CPUs", "%d (fast cores %s)" % (len(sets["all"]), ",".join(map(str, sets["big"])))),
                     ("Temperature", "%.0f C" % t if t is not None else "unknown")):
            r = Adw.ActionRow(title=k, subtitle=v)
            r.add_css_class("property")
            g.add(r)
        pp.add(g)

        g = Adw.PreferencesGroup(title="Hypervisor")
        ver = host.gvm_version()
        for k, v in (("iceland-gvm", ver or ("installed" if host.gvm_installed() else "not installed")),
                     ("Gunyah modules for this kernel", "yes" if host.modules_match() else "no (kernel-iceland q90m+ has them)"),
                     ("/dev/gunyah", "loaded" if host.gunyah_loaded() else "not loaded (loaded at the first start)"),
                     ("Guest kernel", host.guest_kernel_release() or "missing"),
                     ("UEFI firmware", "present" if __import__("os").path.exists(config.FIRMWARE) else "missing"),
                     ("Running VMs", str(backend.running_count()))):
            r = Adw.ActionRow(title=k, subtitle=v)
            r.add_css_class("property")
            g.add(r)
        pp.add(g)

        g = Adw.PreferencesGroup(title="Proven on this tablet",
                                 description="Set by the tests in /etc/iceland-vms/features.json")
        f = host.features()
        for k, label in FEATURE_LABEL.items():
            r = Adw.ActionRow(title=label)
            ok = bool(f.get(k))
            note = host.UNTESTED.get(k) if ok else None
            r.set_subtitle(note or ("enabled" if ok else "not enabled yet"))
            r.add_prefix(Gtk.Image.new_from_icon_name("emblem-ok-symbolic" if ok and not note else
                                                      "dialog-warning-symbolic" if ok else "action-unavailable-symbolic"))
            g.add(r)
        pp.add(g)

        g = Adw.PreferencesGroup(title="Cleanup", description="Only when no VM runs")
        if gvmlib.unload_allowed():
            r = Adw.ActionRow(title="Unload the Gunyah modules")
            r.add_suffix(util.button(label="Unload", cb=lambda: self.run("unload")))
            g.add(r)
        r = Adw.ActionRow(title="Remove the VM network (gvm-tap)")
        r.add_suffix(util.button(label="Remove", cb=lambda: self.run("net-down")))
        g.add(r)
        pp.add(g)

    def run(self, verb):
        util.run_async(backend.helper_argv(verb), lambda ok, out, err: self.win.toast("Done") if ok
                       else util.error(self.win, "Failed", err or out))
