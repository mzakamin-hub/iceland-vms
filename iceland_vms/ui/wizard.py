"""New VM wizard: source (catalogue / ISO / disk image / test shell) -> resources -> create."""
import os
import threading

from gi.repository import Adw, Gio, GLib, Gtk

from .. import catalogue, config, create, download, host
from . import util

STATUS_LABEL = {"proven": "Tested on the tablet", "untested-on-device": "Not yet tested on the tablet"}


class CreateWizard(Adw.Dialog):
    def __init__(self, win):
        super().__init__(title="New Virtual Machine", content_width=640, content_height=720)
        self.win = win
        self.src = None          # dict(kind=catalogue|iso|image|test, entry=..., path=...)
        self.cancel = threading.Event()
        self.nav = Adw.NavigationView()
        self.set_child(self.nav)
        self.nav.add(self._source_page())

    def _page(self, title, content, tag=None, header_end=None):
        tv = Adw.ToolbarView()
        hb = Adw.HeaderBar()
        if header_end:
            hb.pack_end(header_end)
        tv.add_top_bar(hb)
        tv.set_content(content)
        p = Adw.NavigationPage(title=title, child=tv)
        if tag:
            p.set_tag(tag)
        return p

    # ---- 1. source ----
    def _source_page(self):
        pp = Adw.PreferencesPage()
        g = Adw.PreferencesGroup(title="Install from",
                                 description="Virtual machines here are protected Gunyah VMs: the tablet lends them "
                                 "memory it cannot use while they run.")
        for title, sub, icon, cb in (
                ("Catalogue", "Systems prepared for this tablet, downloaded and checksum-verified",
                 "system-software-install-symbolic", lambda: self.nav.push(self._catalogue_page())),
                ("ISO file", "An installer ISO, booted through UEFI firmware (its kernel must support the restricted "
                 "DMA pool)", "media-optical-symbolic", lambda: self._pick_file("iso")),
                ("Disk or cloud image", "A raw / qcow2 / .xz / .zst Linux disk image, booted with the tablet's guest kernel",
                 "drive-harddisk-symbolic", lambda: self._pick_file("image")),
                ("Windows 11 ARM64 image", "Import a set-up Windows disk (win-image-build.ps1): UEFI ECAM firmware, "
                 "software display, Remote Desktop; the image file itself is not changed",
                 "computer-symbolic", lambda: self._pick_file("windows")),
                ("Test shell", "A tiny busybox system on the serial console (the V1 test)",
                 "utilities-terminal-symbolic", lambda: self._go_resources({"kind": "test"}))):
            r = Adw.ActionRow(title=title, subtitle=sub, activatable=True)
            r.add_prefix(Gtk.Image.new_from_icon_name(icon))
            r.add_suffix(Gtk.Image.new_from_icon_name("go-next-symbolic"))
            r.connect("activated", lambda _r, f=cb: f())
            g.add(r)
        pp.add(g)
        if not host.gvm_installed():
            g2 = Adw.PreferencesGroup()
            g2.add(util.info_row("iceland-gvm is not installed", "VMs can be created, but not started.",
                                 "dialog-warning-symbolic"))
            pp.add(g2)
        return self._page("New Virtual Machine", pp)

    def _catalogue_page(self):
        pp = Adw.PreferencesPage()
        entries = catalogue.load()
        for mode, title in (("cloud-direct", "Ready-made images (recommended)"), ("iso-uefi", "Installer ISOs (UEFI)")):
            es = [e for e in entries if e["mode"] == mode]
            if not es:
                continue
            g = Adw.PreferencesGroup(title=title, description=catalogue.MODES[mode])
            for e in es:
                r = Adw.ActionRow(title=e["name"], activatable=True,
                                  subtitle="%s - %s download - %s" % (e.get("version", ""), util.human_size(e["size"]),
                                                                     STATUS_LABEL.get(e.get("status"), e.get("status", ""))))
                r.set_subtitle_lines(2)
                r.add_prefix(Gtk.Image.new_from_icon_name("computer-symbolic"))
                if e.get("status") != "proven":
                    w = Gtk.Image.new_from_icon_name("dialog-warning-symbolic")
                    w.set_tooltip_text(STATUS_LABEL["untested-on-device"])
                    r.add_suffix(w)
                r.add_suffix(Gtk.Image.new_from_icon_name("go-next-symbolic"))
                r.connect("activated", lambda _r, e=e: self._go_resources({"kind": "catalogue", "entry": e}))
                g.add(r)
            pp.add(g)
        if not entries:
            pp.add(Adw.PreferencesGroup(title="The catalogue is empty"))
        g = Adw.PreferencesGroup(title="Why so few?")
        g.add(util.info_row("Only proven combinations are listed",
                            "Installer ISOs work only when the distribution's own kernel has CONFIG_DMA_RESTRICTED_POOL "
                            "(Fedora's does not). Ready-made images run any distribution with the tablet's guest kernel."))
        pp.add(g)
        return self._page("Catalogue", pp)

    def _pick_file(self, kind):
        dlg = Gtk.FileDialog(title={"iso": "Choose an ISO image", "windows": "Choose the Windows disk image"}
                             .get(kind, "Choose a disk image"))
        flt = Gtk.FileFilter()
        if kind == "windows":
            flt.set_name("Raw disk images")
            for p in ("*.raw", "*.img"):
                flt.add_pattern(p)
            imgs = os.path.join(self.win.home, ".local", "share", "gvm", "images")
            if os.path.isdir(imgs):
                dlg.set_initial_folder(Gio.File.new_for_path(imgs))
        elif kind == "iso":
            flt.set_name("ISO images")
            flt.add_pattern("*.iso")
        else:
            flt.set_name("Disk images")
            for p in ("*.raw", "*.img", "*.qcow2", "*.xz", "*.zst"):
                flt.add_pattern(p)
        fl = Gio.ListStore.new(Gtk.FileFilter)
        fl.append(flt)
        dlg.set_filters(fl)

        def done(d, res):
            try:
                f = d.open_finish(res)
            except GLib.Error:
                return
            self._go_resources({"kind": kind, "path": f.get_path()})
        dlg.open(self.win, None, done)

    # ---- 2. resources ----
    def _go_resources(self, src):
        self.src = src
        e = src.get("entry") or {}
        rec = e.get("recommended", {})
        pp = Adw.PreferencesPage()
        g = Adw.PreferencesGroup(title="Virtual machine")
        win = src["kind"] == "windows"
        default_name = "Windows 11" if win else e.get("name") or (
            os.path.splitext(os.path.basename(src["path"]))[0][:40] if src.get("path") else "Test shell")
        self.e_name = Adw.EntryRow(title="Name", text=default_name)
        g.add(self.e_name)
        pp.add(g)
        g = Adw.PreferencesGroup(title="Resources")
        mx = host.max_vcpus()
        test = src["kind"] == "test"
        if win:
            rec = {"cpus": 4, "memory_mib": 4096}        # proven by vm-run WIN (v10l)
        self.e_cpus = util.spin_row("Virtual CPUs", "At most %d" % mx, 1, mx, 1, min(rec.get("cpus", 2 if test else 4), mx))
        g.add(self.e_cpus)
        mmax = max(256, host.max_guest_mib())
        self.e_mem = util.spin_row("Memory (MiB)", "Lent to the VM while it runs; up to %d MiB" % mmax, 256, mmax, 256,
                                   min(rec.get("memory_mib", 512 if test else (4096 if src["kind"] == "iso" else 2048)), mmax))
        g.add(self.e_mem)
        self.e_disk = util.spin_row("Disk size (GiB)", "Sparse: only written data uses space", 4, 512, 1,
                                    rec.get("disk_gib", 32 if src["kind"] == "iso" else 20))
        self.e_disk.set_visible(not test and not win)
        g.add(self.e_disk)
        pp.add(g)
        g = Adw.PreferencesGroup(title="Devices")
        self.e_net = Adw.SwitchRow(title="Network", subtitle="NAT through the tablet's connection", active=not test)
        g.add(self.e_net)
        # 0.2-24: Fedora 44+ cloud images default to the native GPU (msm native context, config.new_vm_gpu)
        native = config.new_vm_gpu(e.get("id", ""), host.features(), win) == "native"
        items = ([("native", "3D: native GPU (GL + Vulkan on the Adreno) - recommended")] if native else []) + [
            ("venus", "3D: OpenGL (virgl) + Vulkan (venus)" + ("" if native else " - recommended")),
            ("simplefb", "Software display (fallback)"),
            ("2d", "2D window (virtio-gpu)"),
            ("none", "None (serial console only)")]
        self.e_gpu = util.combo_row("Display", items, len(items) - 1 if test else 0)
        g.add(self.e_gpu)
        self.e_gpu.set_visible(not win)          # Windows: the software display (simplefb) only
        self.e_user = Adw.EntryRow(title="Windows account for the clean shutdown (ssh key, no password)", text="gvm")
        self.e_user.set_visible(win)
        g.add(self.e_user)
        pp.add(g)
        if win:
            g = Adw.PreferencesGroup(title="Windows")
            g.add(util.info_row("Proven settings", "UEFI firmware with ACPI + PCI ECAM, software display, 2 MiB "
                                    "memory pages, NAT network, io_uring disk. Sign in through Remote Desktop with your "
                                    "Windows password; the app does not store it."))
            pp.add(g)
        if e and e.get("notes"):
            g = Adw.PreferencesGroup(title="Notes")
            g.add(util.info_row(e["name"], e["notes"]))
            pp.add(g)
        btn = util.button(label="Create", css=["suggested-action"], cb=self._create)
        self.nav.push(self._page("Resources", pp, header_end=btn))

    # ---- 3. create ----
    def _create(self):
        name = self.e_name.get_text().strip()[:64] or "VM"
        vals = dict(name=name, cpus=int(self.e_cpus.get_value()), mem=int(self.e_mem.get_value()),
                    disk=int(self.e_disk.get_value()), net=self.e_net.get_active(), gpu=util.combo_key(self.e_gpu),
                    ssh_user=self.e_user.get_text().strip())
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18, valign=Gtk.Align.CENTER,
                      margin_start=36, margin_end=36)
        self.p_label = Gtk.Label(label="Preparing...", wrap=True)
        self.p_label.add_css_class("title-4")
        self.p_bar = Gtk.ProgressBar(show_text=True)
        self.p_detail = Gtk.Label(wrap=True)
        self.p_detail.add_css_class("dim-label")
        self.p_btn = util.button(label="Cancel", css=["pill"], cb=self._cancel_or_close)
        self.p_btn.set_halign(Gtk.Align.CENTER)
        for w in (self.p_label, self.p_bar, self.p_detail, self.p_btn):
            box.append(w)
        page = self._page("Creating", box)
        page.set_can_pop(False)
        self.nav.push(page)
        self.created = None
        util.in_thread(lambda: self._do_create(vals), self._created)

    def _progress(self, label, frac=None, detail=""):
        def up():
            self.p_label.set_label(label)
            if frac is None:
                self.p_bar.pulse()
            else:
                self.p_bar.set_fraction(max(0.0, min(1.0, frac)))
            self.p_detail.set_label(detail)
            return False
        GLib.idle_add(up)

    def _dl_progress(self, stage, done, total):
        if self.cancel.is_set():
            raise download.DownloadError("cancelled")
        word = "Downloading" if stage == "download" else "Verifying checksum"
        self._progress(word, done / total if total else None, "%s of %s" % (util.human_size(done), util.human_size(total or 0)))

    def _do_create(self, v):
        return create.create_vm(self.win.home, self.src, v, progress=self._progress,
                                dl_progress=self._dl_progress, cancel=self.cancel)

    def _created(self, cfg, err):
        if err:
            self.p_label.set_label("Could not create the virtual machine")
            self.p_detail.set_label(err)
            self.p_btn.set_label("Close")
            self.created = None
            return
        self.created = cfg
        self.p_bar.set_fraction(1.0)
        self.p_label.set_label("%s is ready" % cfg["name"])
        notes = []
        if cfg["cloud_init"]["enabled"]:
            notes.append("User %s; the password is in the VM's overview." % cfg["cloud_init"]["user"])
        if config.is_windows(cfg):
            notes.append("Start it, then use the Remote Desktop button once it has an address.")
        elif cfg["boot"]["mode"] == "uefi":
            notes.append("After installing, remove the ISO in Settings > Storage.")
        self.p_detail.set_label(" ".join(notes))
        self.p_btn.set_label("Done")
        self.win.reload(select=cfg["id"])

    def _cancel_or_close(self):
        if self.p_btn.get_label() == "Cancel":
            self.cancel.set()
            self.p_label.set_label("Cancelling...")
        else:
            self.close()
