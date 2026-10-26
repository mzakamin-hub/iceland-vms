"""Per-VM settings editor: every resource crosvm supports for protected Gunyah VMs.

Things that cannot work (USB / PCI passthrough with crosvm, bridged Wi-Fi, clipboard, live
snapshots) are shown as explanations instead of switches.
"""
import copy
import os

from gi.repository import Adw, Gio, GLib, Gtk

from .. import backend, config, host, imageprobe, storage
from . import util


def monitors():
    """[(connector, label, width_px, height_px)] of the host's monitors (GDK; the connector is crosvm's wl_output
    name, the size in device pixels = logical size x scale)."""
    out = []
    try:
        from gi.repository import Gdk
        mons = Gdk.Display.get_default().get_monitors()
        for i in range(mons.get_n_items()):
            m = mons.get_item(i)
            con = m.get_connector() or ""
            if con:
                g = m.get_geometry()
                sc = m.get_scale() if hasattr(m, "get_scale") else m.get_scale_factor()
                pw, ph = int(round(g.width * sc)), int(round(g.height * sc))
                desc = m.get_description() or m.get_model() or con
                out.append((con, "%s (%s, %dx%d)" % (con, desc, pw, ph), pw, ph))
    except Exception:  # noqa: BLE001 - no display (tests) or an old GDK
        pass
    return out


def native_size(cfg):
    """The guest size for a fullscreen VM at the monitor's resolution, or None (config.fullscreen_size)."""
    return config.fullscreen_size(cfg, [(m[0], m[2], m[3]) for m in monitors()])

# 3D default (24 Oct, V4 + V4-5/V4-6 PASS): "venus" = OpenGL through virgl + Vulkan through venus
GPU_ITEMS = [("venus", "3D: OpenGL (virgl) + Vulkan (venus) - recommended"),
             ("simplefb", "Software display (fallback, like DroidVM)"),
             ("none", "None (serial console only)"), ("2d", "2D window (virtio-gpu)"),
             ("virgl", "3D: OpenGL only (virgl)"),
             ("zink", "3D: Vulkan (venus), OpenGL through zink (experimental, DroidVM-style)"),
             ("native", "3D: msm native context (experimental, falls back to llvmpipe)")]
GPU_FEATURE = {"virgl": "gpu3d_virgl", "native": "gpu3d_native", "venus": "gpu3d_venus", "zink": "gpu3d_venus",
               "simplefb": "simplefb"}


class SettingsPage(Gtk.Box):
    def __init__(self, win, cfg, on_saved):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.win, self.on_saved = win, on_saved
        self.cfg = copy.deepcopy(cfg)
        self.feats = host.features()
        self.page = Adw.PreferencesPage(vexpand=True)
        self.banner = Adw.Banner(title="Changes apply at the next start of the VM")
        self.append(self.banner)
        self.append(self.page)
        bar = Gtk.Box(spacing=12, margin_top=8, margin_bottom=8, margin_start=12, margin_end=12, halign=Gtk.Align.END)
        bar.append(util.button(label="Revert", cb=self.revert))
        bar.append(util.button(label="Apply", css=["suggested-action"], cb=self.apply))
        self.append(bar)
        self.build()

    # ---- building ----
    def build(self):
        c, f, p = self.cfg, self.feats, self.page
        self.banner.set_revealed(backend.state(c["id"])["state"] not in ("stopped", "failed"))

        g = Adw.PreferencesGroup(title="General")
        self.name = Adw.EntryRow(title="Name", text=c["name"])
        g.add(self.name)
        self.notes = Adw.EntryRow(title="Notes", text=c.get("notes", ""))
        g.add(self.notes)
        self.autostart = Adw.SwitchRow(title="Start automatically",
                                       subtitle="When you log in (one VM at a time)", active=c["autostart"])
        g.add(self.autostart)
        auto = config.BACKEND_LABEL[config.resolve_backend(c, f)]
        self.backend = util.combo_row("Backend", [("auto", "Automatic (%s)" % auto), ("crosvm", "crosvm"),
                                                  ("qemu", "QEMU-Gunyah")],
                                      subtitle="Automatic: QEMU-Gunyah only for USB passthrough, crosvm otherwise. "
                                      "QEMU-Gunyah runs 3D OpenGL (about 80 % of crosvm's speed), not Vulkan")
        util.combo_set(self.backend, c["backend"])
        # without a usable QEMU-Gunyah there is nothing to choose: crosvm only
        self.backend.set_visible(bool(f["qemu_backend"]) or c["backend"] == "qemu")
        g.add(self.backend)
        self.conflict = Adw.ActionRow(title="", subtitle="")
        self.conflict.set_subtitle_lines(0)
        self.conflict.add_prefix(Gtk.Image.new_from_icon_name("dialog-warning-symbolic"))
        self.conflict_btn = util.button(label="Switch", css=["suggested-action"], cb=self.switch_backend)
        self.conflict.add_suffix(self.conflict_btn)
        g.add(self.conflict)
        self.backend.connect("notify::selected", lambda *_: self.update_conflict())
        p.add(g)

        g = Adw.PreferencesGroup(title="Processor")
        mx = host.max_vcpus()
        self.cpus = util.spin_row("Virtual CPUs", "At most %d: two host CPUs stay free" % mx, 1, mx, 1, min(c["cpus"], mx))
        g.add(self.cpus)
        sets = host.cpu_sets()
        self.aff = util.combo_row("Run on", [("any", "Any host CPU"),
                                             ("big", "Fast cores (%s)" % ",".join(map(str, sets["big"]))),
                                             ("little", "Efficient cores (%s)" % ",".join(map(str, sets["little"]))),
                                             ("custom", "Custom CPU list")],
                                  subtitle="Pins the vCPU threads (crosvm --cpu-affinity)")
        util.combo_set(self.aff, c["cpu_affinity"])
        g.add(self.aff)
        self.cpuset = Adw.EntryRow(title="Host CPUs (e.g. 0-5)", text=c["cpu_set"])
        self.cpuset.set_visible(c["cpu_affinity"] == "custom")
        self.aff.connect("notify::selected", lambda *_: self.cpuset.set_visible(util.combo_key(self.aff) == "custom"))
        g.add(self.cpuset)
        p.add(g)

        g = Adw.PreferencesGroup(title="Memory",
                                 description="Guest memory is lent to the hypervisor: the host cannot use or swap it "
                                 "while the VM runs. %d GiB always stay for the tablet." % (config.HOST_RESERVE_MIB // 1024))
        mmax = host.max_guest_mib(c["swiotlb_mib"])
        self.mem = util.spin_row("Memory (MiB)", "Up to %d MiB on this tablet" % mmax, 256, max(mmax, 256), 256,
                                 min(c["memory_mib"], max(mmax, 256)))
        g.add(self.mem)
        self.swiotlb = util.spin_row("I/O bounce buffer (MiB)",
                                     "Shared swiotlb for all virtio I/O; 128 is enough, 256 for 3D or heavy disk use (at most 320: the "
                                     "guest kernel cannot set up a larger pool)",
                                     config.LIMITS["swiotlb_mib"][0], config.LIMITS["swiotlb_mib"][1], 64,
                                     min(c["swiotlb_mib"], config.LIMITS["swiotlb_mib"][1]))
        g.add(self.swiotlb)
        self.huge = Adw.SwitchRow(title="2 MiB guest memory pages (recommended)",
                                  subtitle="Guest memory is handed to the hypervisor in 2 MiB pieces: 512 times fewer "
                                  "secure-world calls. Off = 4 KiB pieces, which froze the tablet with Windows",
                                  active=c["hugepages"])
        g.add(self.huge)
        # 0.2-12: crosvm's disk engine (vm-run OPT v10m: io_uring 3.3 GB/s read vs 0.7 with epoll)
        self.aio = util.combo_row("Disk I/O engine", [("uring", "io_uring (recommended)"), ("epoll", "epoll (the old default)")],
                                  0 if c.get("disk_async", "uring") == "uring" else 1)
        g.add(self.aio)
        # 0.2-10 (step 4): opt-in shared memory mode, off by default, with the warning always next to it
        self.memmode = util.combo_row("Memory protection", [
            ("protected", "Protected: the tablet cannot read the VM's memory (default)"),
            ("shared", "Shared with the tablet: NOT protected")])
        util.combo_set(self.memmode, c.get("memory_mode", "protected"))
        g.add(self.memmode)
        self.memmode_warn = util.info_row("The tablet can read this VM's memory", config.MEMORY_SHARED_WARNING
                                          + ("" if f.get("memory_shared") else
                                             " Not enabled on this tablet yet (feature memory_shared)."),
                                          "dialog-warning-symbolic")
        g.add(self.memmode_warn)

        def _memmode_changed(*_):
            shared = util.combo_key(self.memmode) == "shared"
            self.memmode_warn.set_visible(shared)
            self.swiotlb.set_sensitive(not shared)
            self._shared_gpu_guard()
        self.memmode.connect("notify::selected", _memmode_changed)
        _memmode_changed()
        self.mem_note = util.info_row("Larger than tested",
                                      "Guests above %d MiB have not been run on this tablet yet (other Gunyah hosts "
                                      "ran out of memory above ~1 GiB with an older driver). Report the result."
                                      % f["max_proven_memory_mib"], "dialog-warning-symbolic")
        g.add(self.mem_note)
        self.mem_note.set_visible(c["memory_mib"] > f["max_proven_memory_mib"])
        self.mem.connect("notify::value", lambda *_: self.mem_note.set_visible(
            self.mem.get_value() > f["max_proven_memory_mib"]))
        p.add(g)

        g = Adw.PreferencesGroup(title="Boot")
        self.bootmode = util.combo_row("Boot", [("direct", "Tablet guest kernel (direct boot)"),
                                                ("uefi", "UEFI firmware (the system's own kernel)")])
        util.combo_set(self.bootmode, c["boot"]["mode"])
        g.add(self.bootmode)
        self.root = Adw.EntryRow(title="Root device (PARTUUID=... or /dev/vda2)", text=c["boot"]["root"])
        det = util.button(label="Detect", css=["flat"], cb=self.detect_root)
        self.root.add_suffix(det)
        g.add(self.root)
        self.rootfstype = Adw.EntryRow(title="Root filesystem type", text=c["boot"]["rootfstype"])
        g.add(self.rootfstype)
        self.rootflags = Adw.EntryRow(title="Root mount options (e.g. subvol=root)", text=c["boot"]["rootflags"])
        g.add(self.rootflags)
        self.cmdline = Adw.EntryRow(title="Extra kernel parameters", text=c["boot"]["cmdline_extra"])
        g.add(self.cmdline)
        self.initrd = Adw.SwitchRow(title="Test shell instead of a disk",
                                    subtitle="Boots the busybox test initramfs (as in the V1 test)", active=c["boot"]["initrd"])
        g.add(self.initrd)

        self.fwdebug = Adw.SwitchRow(title="Firmware debug log",
                                     subtitle="Verbose UEFI messages on the serial console (for diagnosing boots)",
                                     active=c["boot"]["firmware_debug"])
        g.add(self.fwdebug)
        g.add(util.info_row("UEFI boot needs the system's own kernel to support protected VMs",
                            "Only kernels with CONFIG_DMA_RESTRICTED_POOL can use the disks (Ubuntu 25.10+, NixOS 26.05; "
                            "not Fedora 44, Debian 13, Arch, openSUSE, Alpine, RHEL clones). Boot entries are not stored: "
                            "installed systems start through EFI\\BOOT\\BOOTAA64.EFI."))

        def boot_vis(*_):
            d = util.combo_key(self.bootmode) == "direct"
            for w in (self.root, self.rootfstype, self.rootflags, self.cmdline, self.initrd):
                w.set_visible(d)
            self.fwdebug.set_visible(not d)
        self.bootmode.connect("notify::selected", boot_vis)
        boot_vis()
        p.add(g)

        self.disk_group = Adw.PreferencesGroup(title="Storage",
                                               description="virtio-blk disks; ISO images are attached read-only")
        add = Gtk.MenuButton(icon_name="list-add-symbolic", tooltip_text="Add storage")
        add.add_css_class("flat")
        m = Gio.Menu()
        m.append("New Empty Disk...", "set.newdisk")
        m.append("Attach ISO...", "set.iso")
        m.append("Attach Existing Disk Image...", "set.image")
        add.set_menu_model(m)
        ag = Gio.SimpleActionGroup()
        for n, cb in (("newdisk", self.add_new_disk), ("iso", lambda: self.add_file("isos")),
                      ("image", lambda: self.add_file("images"))):
            a = Gio.SimpleAction.new(n, None)
            a.connect("activate", lambda *_a, f=cb: f())
            ag.add_action(a)
        self.insert_action_group("set", ag)
        self.disk_group.set_header_suffix(add)
        p.add(self.disk_group)
        self.disk_rows = []
        self.fill_disks()

        g = Adw.PreferencesGroup(title="Network")
        self.net = util.combo_row("Network", [("off", "Off"), ("nat", "NAT through the tablet's connection")])
        util.combo_set(self.net, c["network"]["mode"])
        g.add(self.net)
        self.mac = Adw.EntryRow(title="MAC address", text=c["network"]["mac"])
        self.mac.add_suffix(util.button(icon="view-refresh-symbolic", css=["flat"], tooltip="New random address",
                                        cb=lambda: self.mac.set_text(config.random_mac())))
        g.add(self.mac)
        self.vhost = Adw.SwitchRow(title="In-kernel network (vhost-net)",
                                   subtitle="The tablet kernel moves the network packets instead of crosvm: "
                                   "less CPU, more throughput. Needs a kernel with vhost-net (q90s or newer)",
                                   active=c["network"].get("vhost_net", True))
        g.add(self.vhost)
        g.add(util.info_row("Bridged network not available",
                            "A Wi-Fi station cannot be bridged and the tablet has no Ethernet: guests use NAT "
                            "(10.250.0.0/24). Use port forwards to reach a guest from the local network."))
        p.add(g)
        self.fw_group = Adw.PreferencesGroup(title="Port forwards",
                                             description="Host port -> guest port while the VM runs: from other devices "
                                             "via the tablet's Wi-Fi address, and from the tablet as localhost:<port>.")
        self.fw_group.set_header_suffix(util.button(icon="list-add-symbolic", css=["flat"], tooltip="Add forward",
                                                    cb=self.add_forward))
        p.add(self.fw_group)
        self.fw_rows = []
        self.fill_forwards()

        g = Adw.PreferencesGroup(title="Display and input")
        items = [(k, lbl + ("" if k not in GPU_FEATURE or f.get(GPU_FEATURE[k]) else " - not proven yet"))
                 for k, lbl in GPU_ITEMS]
        self.gpu = util.combo_row("Graphics", items,
                                  subtitle="3D needs iceland-gvm 0.2 and is enabled once the V4 test passes")
        util.combo_set(self.gpu, c["display"]["gpu"])
        g.add(self.gpu)
        # 0.2-19: no 3D with the shared memory mode: the display falls back to the software display, with the reason;
        # 0.2-20: 3D OpenGL is allowed there (feature memory_shared_3d), Vulkan / native still fall back
        self.shared_gpu_note = util.info_row("Software display: this 3D mode is not available with shared memory",
                                             (config.SHARED_3D_REASON if f.get("memory_shared_vulkan")
                                              else config.SHARED_VULKAN_REASON) if f.get("memory_shared_3d")
                                             else config.SHARED_3D_FEATURE_REASON, "dialog-information-symbolic")
        self.shared_gpu_note.set_visible(False)
        g.add(self.shared_gpu_note)
        self.gpu.connect("notify::selected", lambda *_: self._shared_gpu_guard())
        self._shared_gpu_guard()
        self.width = util.spin_row("Width", None, 640, 3840, 8, c["display"]["width"])
        self.height = util.spin_row("Height", None, 480, 3000, 8, c["display"]["height"])
        self.dpi = util.spin_row("DPI (0 = default)", "Larger text in the guest on the tablet's dense screen", 0, 600, 10,
                                 c["display"]["dpi"])
        self.kbd = Adw.SwitchRow(title="Keyboard in the window", active=c["display"]["keyboard"])
        self.kgrab = Adw.SwitchRow(title="Send shortcuts to the VM",
                                   subtitle="Super, Alt+Tab and other desktop shortcuts go to the guest while its window "
                                            "has the focus (the desktop may ask once; its own escape shortcut ends it)",
                                   active=c["display"].get("keyboard_grab", False))
        self.mouse = Adw.SwitchRow(title="Mouse and touch in the window",
                                   subtitle="Mouse and touch on the window reach the guest", active=c["display"]["mouse"])
        self.inmode = util.combo_row("Touch input", [
            ("tablet", "Tablet: tap clicks, long press = right click, two fingers scroll"),
            ("touchpad", "Touchpad: drag moves the pointer, tap clicks, two fingers scroll"),
            ("touch", "Touch screen: the guest's own multi-touch gestures")])
        util.combo_set(self.inmode, c["display"].get("input_mode", "tablet"))
        self.fullscreen = Adw.SwitchRow(title="Open fullscreen", active=c["display"].get("fullscreen", False),
                                        subtitle="Leave or enter fullscreen from the VM page while it runs")
        self.fsnative = Adw.SwitchRow(title="Fullscreen at the monitor's resolution",
                                      subtitle="The guest display gets the monitor's full resolution; off = the "
                                      "width and height above, scaled to the screen",
                                      active=c["display"].get("fullscreen_native", True))
        mons = [("", "Any monitor")] + [(m[0], m[1]) for m in monitors()]
        cur = c["display"].get("output", "")
        if cur and cur not in [k for k, _ in mons]:
            mons.append((cur, cur + " (not connected)"))
        self.output = util.combo_row("Monitor for fullscreen", mons)
        util.combo_set(self.output, cur)
        self.autologin = Adw.SwitchRow(title="Log in automatically",
                                       subtitle="Desktop guests start logged in: touch and the on-screen keyboard "
                                       "cannot type into a login screen inside the VM window", active=c["display"]["autologin"])
        self.glamor = Adw.SwitchRow(title="Accelerated X (glamor)",
                                    subtitle="On (recommended): the guest desktop is composited on the GPU (virgl). "
                                    "Off: the desktop is drawn by the CPU; 3D apps still use the GPU",
                                    active=c["display"]["glamor"])
        self.session = util.combo_row("Guest desktop session (3D)", [("x11", "Xorg (glamor) - recommended"),
                                                                       ("wayland", "Wayland (labwc)")],
                                      1 if c["display"].get("session", "x11") == "wayland" else 0)
        self.session.set_subtitle("Wayland starts labwc with a terminal instead of the Xorg desktop (needs labwc "
                                  "in the guest; the display manager is bypassed for that boot)")
        for w in (self.width, self.height, self.dpi, self.kbd, self.kgrab, self.mouse, self.inmode, self.fullscreen, self.fsnative,
                  self.output, self.autologin, self.session, self.glamor):
            g.add(w)
        p.add(g)

        # 0.2-7 (DroidVM parity): the display over VNC instead of the window
        d = c["display"]
        g = Adw.PreferencesGroup(title="Remote display (VNC)",
                                 description="The VM's screen is served over VNC instead of a window: connect with "
                                 "KRDC, TigerVNC or any VNC viewer (the VM page has a button for it). Without a "
                                 "window the VM also runs without a logged-in session.")
        self.vnc = Adw.SwitchRow(title="Show the display over VNC", active=d.get("vnc", False))
        self.vnclan = Adw.SwitchRow(title="Reachable from other computers",
                                    subtitle="Listens on every network address (needs a password; open the port in "
                                    "the firewall yourself). Off = this tablet only (127.0.0.1)",
                                    active=d.get("vnc_lan", False))
        self.vncport = util.spin_row("Port", "VNC display :N = port 5900 + N", 5900, 5999, 1, d.get("vnc_port", 5900))
        self.vncpwon = Adw.SwitchRow(title="Password", subtitle="VNC passwords count 8 characters at most and are "
                                     "weakly protected: use them only on a trusted network",
                                     active=d.get("vnc_password", False))
        self.vncpw = Adw.PasswordEntryRow(title="New password (empty = keep the current one)")
        for w in (self.vnc, self.vnclan, self.vncport, self.vncpwon, self.vncpw):
            g.add(w)
        p.add(g)

        g = Adw.PreferencesGroup(title="Sound")
        self.sound = util.combo_row("Sound", [("off", "Off"), ("null", "Virtual sound card, silent"),
                                              ("file", "Virtual sound card, recorded to a file")],
                                    subtitle="crosvm has no PipeWire output: guest sound cannot be heard yet")
        util.combo_set(self.sound, c["sound"]["mode"])
        self.sound.set_sensitive(f["sound"])
        g.add(self.sound)
        p.add(g)

        self.sh_group = Adw.PreferencesGroup(title="Shared folders",
                                             description="Folders of your home shared with the guest. In the guest: "
                                             "mount -t 9p -o trans=virtio TAG /mnt (virtio-fs: mount -t virtiofs TAG /mnt). "
                                             "Files the guest writes belong to you.")
        self.sh_group.set_header_suffix(util.button(icon="list-add-symbolic", css=["flat"], tooltip="Share a folder",
                                                    cb=self.add_share))
        p.add(self.sh_group)
        self.sh_rows = []
        self.fill_shares()

        g = Adw.PreferencesGroup(title="Guest integration")
        self.ci = Adw.SwitchRow(title="cloud-init", subtitle="User, password, ssh key and hostname for cloud images",
                                active=c["cloud_init"]["enabled"])
        g.add(self.ci)
        self.ciuser = Adw.EntryRow(title="Guest user name", text=c["cloud_init"]["user"])
        g.add(self.ciuser)
        self.tools = Adw.SwitchRow(title="Guest kernel modules share",
                                   subtitle="Installs the tablet guest kernel's modules at every boot (9p tag gvmshare); "
                                   "also carries gvm-guest-kernel-install.sh for UEFI guests", active=c["guest_tools"])
        g.add(self.tools)
        p.add(g)

        g = Adw.PreferencesGroup(title="USB devices", description="Passthrough runs the VM with QEMU-Gunyah "
                                 "(Automatic switches to it when a device is selected)")
        if f["qemu_backend"]:
            self.usb_rows = []
            for dev in backend.usb_devices():
                r = Adw.SwitchRow(title=dev["name"], subtitle="%s:%s" % (dev["vendor"], dev["product"]))
                r.set_active(any(u["vendor"] == dev["vendor"] and u["product"] == dev["product"] for u in c["usb"]))
                r.dev = dev
                r.connect("notify::active", lambda *_: self.update_conflict())
                self.usb_rows.append(r)
                g.add(r)
            if not self.usb_rows:
                g.add(Adw.ActionRow(title="No USB devices connected"))
        else:
            self.usb_rows = None
            g.add(util.info_row("USB passthrough not available",
                                "crosvm turns USB off for protected VMs (their memory is lent to the hypervisor). "
                                "It needs the QEMU-Gunyah backend, which is not enabled on this tablet."))
        p.add(g)

        g = Adw.PreferencesGroup(title="Not possible on this tablet")
        for t, s in (("PCI / GPU passthrough", "Gunyah gives the host no IOMMU control (no VFIO)."),
                     ("Saving a running VM (live snapshot, suspend to disk)",
                      "The host cannot read a protected VM's memory. Snapshots are taken while the VM is stopped."),
                     ("Shared clipboard", "No guest agent channel in crosvm's display window."),
                     ("Nested virtualization", "Gunyah owns EL2: no KVM in the host or the guest.")):
            g.add(util.info_row(t, s, "action-unavailable-symbolic"))
        p.add(g)
        self.gpu.connect("notify::selected", lambda *_: self.update_conflict())
        self.update_conflict()

    # ---- backend ----
    def update_conflict(self):
        """Show what the chosen backend cannot do, and offer the backend that can."""
        try:
            c = self.collect()
        except Exception:
            return
        rb = config.resolve_backend(c, self.feats)
        self.backend.get_model().splice(0, 1, ["Automatic (%s)" % config.BACKEND_LABEL[
            config.resolve_backend(dict(c, backend="auto"), self.feats)]])
        bad = config.backend_conflicts(c, rb, self.feats)
        other = "crosvm" if rb == "qemu" else "qemu"
        can_switch = bad and not config.backend_conflicts(dict(c, backend=other), other, self.feats) and             (other == "crosvm" or self.feats["qemu_backend"])
        self.conflict.set_visible(bool(bad))
        if bad:
            self.conflict.set_title("%s cannot provide %s" % (config.BACKEND_LABEL[rb], ", ".join(bad)))
            self.conflict.set_subtitle("Switch to %s, or turn these off." % config.BACKEND_LABEL[other]
                                       if can_switch else "Turn these off: no backend provides all of them.")
            self.conflict_btn.set_visible(bool(can_switch))
            self._switch_to = other

    def switch_backend(self):
        util.combo_set(self.backend, self._switch_to)
        self.update_conflict()

    # ---- disks ----
    def fill_disks(self):
        for r in self.disk_rows:
            self.disk_group.remove(r)
        self.disk_rows = []
        for i, d in enumerate(self.cfg["disks"]):
            path = storage.disk_path(self.win.home, self.cfg, d)
            try:
                size = util.human_size(os.path.getsize(path))
            except OSError:
                size = "missing"
            kind = "ISO" if d["kind"] == "cdrom" else "Disk"
            r = Adw.ActionRow(title=d["name"], subtitle="%s, %s%s, vd%s" % (kind, size, ", read-only" if d["ro"] else "",
                                                                            chr(97 + i)))
            r.add_prefix(Gtk.Image.new_from_icon_name("media-optical-symbolic" if d["kind"] == "cdrom"
                                                      else "drive-harddisk-symbolic"))
            if d["kind"] == "disk" and d["store"] == "vm":
                r.add_suffix(util.button(icon="zoom-fit-best-symbolic", css=["flat"], tooltip="Grow disk",
                                         cb=lambda i=i: self.resize_disk(i)))
            r.add_suffix(util.button(icon="list-remove-symbolic", css=["flat"], tooltip="Remove from the VM",
                                     cb=lambda i=i: self.remove_disk(i)))
            self.disk_rows.append(r)
            self.disk_group.add(r)
        if not self.cfg["disks"]:
            r = Adw.ActionRow(title="No storage")
            self.disk_rows.append(r)
            self.disk_group.add(r)

    def add_new_disk(self):
        d = Adw.AlertDialog.new("New empty disk", "A sparse raw disk: it only uses the space the guest writes.")
        spin = Gtk.SpinButton.new_with_range(1, config.LIMITS["disk_gib"][1], 1)
        spin.set_value(20)
        box = Gtk.Box(spacing=12)
        box.append(Gtk.Label(label="Size (GiB)"))
        box.append(spin)
        d.set_extra_child(box)
        d.add_response("cancel", "Cancel")
        d.add_response("add", "Add")
        d.set_response_appearance("add", Adw.ResponseAppearance.SUGGESTED)

        def resp(_d, r):
            if r != "add":
                return
            vd = config.vm_dir(self.win.home, self.cfg["id"])
            n = 0
            while os.path.exists(os.path.join(vd, "disk%d.raw" % n)) or \
                    any(x["name"] == "disk%d.raw" % n for x in self.cfg["disks"]):
                n += 1
            name = "disk%d.raw" % n
            try:
                storage.create_disk(os.path.join(vd, name), int(spin.get_value()))
            except OSError as e:
                util.error(self.win, "Cannot create the disk", str(e))
                return
            self.cfg["disks"].append({"store": "vm", "name": name, "kind": "disk", "ro": False})
            self.fill_disks()
        d.connect("response", resp)
        d.present(self.win)

    def add_file(self, store):
        dlg = Gtk.FileDialog(title="Attach ISO image" if store == "isos" else "Attach disk image")
        flt = Gtk.FileFilter()
        if store == "isos":
            flt.set_name("ISO images")
            flt.add_pattern("*.iso")
        else:
            flt.set_name("Disk images")
            for pat in ("*.raw", "*.img", "*.qcow2", "*.xz", "*.zst"):
                flt.add_pattern(pat)
        fl = Gio.ListStore.new(Gtk.FileFilter)
        fl.append(flt)
        dlg.set_filters(fl)

        def done(d, res):
            try:
                gf = d.open_finish(res)
            except GLib.Error:
                return
            src = gf.get_path()
            self.win.toast("Copying %s..." % os.path.basename(src))
            util.in_thread(lambda: self._import(src, store), self._imported)
        dlg.open(self.win, None, done)

    def _import(self, src, store):
        name = os.path.basename(src)
        if not config.FILE_RE.match(name):
            name = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in name).lstrip("._-")[:120] or "image"
        if store == "isos":
            dst = os.path.join(config.store_dir(self.win.home, "isos"), name)
            if not os.path.exists(dst):
                try:
                    os.link(src, dst)               # same filesystem: no copy
                except OSError:
                    storage._reflink(src, dst)
            return {"store": "isos", "name": name, "kind": "cdrom", "ro": True}
        base = os.path.splitext(name)[0] if name.endswith((".xz", ".zst", ".qcow2")) else name
        base = base[:-6] if base.endswith(".qcow2") else base
        dst = os.path.join(config.vm_dir(self.win.home, self.cfg["id"]), base + (".raw" if not base.endswith(".raw") else ""))
        storage.import_image(src, dst)
        return {"store": "vm", "name": os.path.basename(dst), "kind": "disk", "ro": False}

    def _imported(self, disk, err):
        if err:
            util.error(self.win, "Import failed", err)
            return
        self.cfg["disks"].append(disk)
        if disk["kind"] == "cdrom":
            self.cfg["disks"].sort(key=lambda d: d["kind"] != "cdrom")    # boot media first
        self.fill_disks()
        self.win.toast("Attached %s" % disk["name"])

    def remove_disk(self, i):
        d = self.cfg["disks"][i]

        def go(resp):
            if resp in ("keep", "delete"):
                self.cfg["disks"].pop(i)
                if resp == "delete" and d["store"] == "vm":
                    try:
                        os.unlink(storage.disk_path(self.win.home, self.cfg, d))
                    except OSError:
                        pass
                self.fill_disks()
        resp = [("cancel", "Cancel"), ("keep", "Detach")]
        if d["store"] == "vm":
            resp.append(("delete", "Delete File"))
        util.alert(self.win, "Remove %s?" % d["name"], "Detach keeps the file; Delete removes it permanently.",
                   tuple(resp), default="cancel", destructive="delete" if d["store"] == "vm" else None, callback=go)

    def resize_disk(self, i):
        d = self.cfg["disks"][i]
        path = storage.disk_path(self.win.home, self.cfg, d)
        cur = max(1, -(-os.path.getsize(path) // (1 << 30)))
        dlg = Adw.AlertDialog.new("Grow %s" % d["name"], "Disks can only grow. cloud-init grows the guest's "
                                  "partition at the next boot; otherwise use growpart / resize2fs in the guest.")
        spin = Gtk.SpinButton.new_with_range(cur, config.LIMITS["disk_gib"][1], 1)
        spin.set_value(cur)
        dlg.set_extra_child(spin)
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("grow", "Grow")

        def resp(_d, r):
            if r == "grow":
                try:
                    storage.resize_disk(path, int(spin.get_value()))
                except OSError as e:
                    util.error(self.win, "Cannot grow the disk", str(e))
                self.fill_disks()
        dlg.connect("response", resp)
        dlg.present(self.win)

    def detect_root(self):
        disks = [d for d in self.cfg["disks"] if d["kind"] == "disk"]
        for d in disks:
            res = imageprobe.probe(storage.disk_path(self.win.home, self.cfg, d))
            if res["root"]:
                self.root.set_text(res["root"])
                self.rootfstype.set_text(res["rootfstype"])
                self.rootflags.set_text(res["rootflags"])
                self.win.toast("Root found on %s: %s %s" % (d["name"], res["rootfstype"], res["rootflags"]))
                return
        util.error(self.win, "No root filesystem found", "None of the disks has a Linux root filesystem the "
                   "guest kernel can mount directly (ext4, xfs, btrfs).")

    # ---- forwards ----
    def fill_forwards(self):
        for r in self.fw_rows:
            self.fw_group.remove(r)
        self.fw_rows = []
        for i, fw in enumerate(self.cfg["network"]["forwards"]):
            r = Adw.ActionRow(title="%s %d -> guest %d" % (fw["proto"].upper(), fw["host"], fw["guest"]))
            r.add_suffix(util.button(icon="list-remove-symbolic", css=["flat"],
                                     cb=lambda i=i: (self.cfg["network"]["forwards"].pop(i), self.fill_forwards())))
            self.fw_rows.append(r)
            self.fw_group.add(r)

    def add_forward(self):
        d = Adw.AlertDialog.new("Port forward", "Host ports below 1024 are not allowed.")
        box = Gtk.Box(spacing=8)
        proto = Gtk.DropDown.new_from_strings(["tcp", "udp"])
        hp = Gtk.SpinButton.new_with_range(1024, 65535, 1)
        hp.set_value(2222)
        gp = Gtk.SpinButton.new_with_range(1, 65535, 1)
        gp.set_value(22)
        for w in (proto, Gtk.Label(label="host"), hp, Gtk.Label(label="guest"), gp):
            box.append(w)
        d.set_extra_child(box)
        d.add_response("cancel", "Cancel")
        d.add_response("add", "Add")

        def resp(_d, r):
            if r == "add":
                self.cfg["network"]["forwards"].append({"proto": ["tcp", "udp"][proto.get_selected()],
                                                        "host": int(hp.get_value()), "guest": int(gp.get_value())})
                self.fill_forwards()
        d.connect("response", resp)
        d.present(self.win)

    # ---- shares ----
    def fill_shares(self):
        for r in self.sh_rows:
            self.sh_group.remove(r)
        self.sh_rows = []
        for i, sh in enumerate(self.cfg["shares"]):
            r = Adw.ActionRow(title=sh["path"], subtitle="tag %s, %s, %s" % (sh["tag"], "9p" if sh["kind"] == "p9" else "virtio-fs",
                                                                            "read-only" if sh["ro"] else "read-write"))
            r.add_prefix(Gtk.Image.new_from_icon_name("folder-remote-symbolic"))
            r.add_suffix(util.button(icon="list-remove-symbolic", css=["flat"],
                                     cb=lambda i=i: (self.cfg["shares"].pop(i), self.fill_shares())))
            self.sh_rows.append(r)
            self.sh_group.add(r)

    def add_share(self):
        dlg = Gtk.FileDialog(title="Share a folder with the guest")

        def done(d, res):
            try:
                gf = d.select_folder_finish(res)
            except GLib.Error:
                return
            path = os.path.normpath(gf.get_path())
            if not (path + "/").startswith(self.win.home.rstrip("/") + "/"):
                util.error(self.win, "Folder not allowed", "Only folders inside your home folder can be shared.")
                return
            a = Adw.AlertDialog.new("Share %s" % os.path.basename(path), path)
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            kind = Gtk.DropDown.new_from_strings(["9p (works everywhere)", "virtio-fs (faster)"])
            ro = Gtk.CheckButton(label="Read-only", active=True)
            box.append(kind)
            box.append(ro)
            a.set_extra_child(box)
            a.add_response("cancel", "Cancel")
            a.add_response("add", "Share")

            def resp(_a, r):
                if r != "add":
                    return
                n = len(self.cfg["shares"])
                tag = "share%d" % n
                while any(s["tag"] == tag for s in self.cfg["shares"]):
                    n += 1
                    tag = "share%d" % n
                self.cfg["shares"].append({"path": path, "tag": tag, "kind": ["p9", "fs"][kind.get_selected()],
                                           "ro": ro.get_active()})
                self.fill_shares()
            a.connect("response", resp)
            a.present(self.win)
        dlg.select_folder(self.win, None, done)

    # ---- apply ----
    def _shared_gpu_guard(self):
        """Shared memory + a 3D display it cannot run: switch to the software display and say why (0.2-19; 0.2-20:
        3D OpenGL stays when the tablet has memory_shared_3d)."""
        if not hasattr(self, "gpu") or not hasattr(self, "memmode"):
            return                                  # called while the pages are still being built
        shared = util.combo_key(self.memmode) == "shared"
        refused = (config.SHARED_3D_BLOCKED + (() if self.feats.get("memory_shared_vulkan") else config.SHARED_VULKAN)
                   if self.feats.get("memory_shared_3d") else config.GPU_3D)
        if shared and util.combo_key(self.gpu) in refused:
            util.combo_set(self.gpu, "simplefb")
            self.shared_gpu_note.set_visible(True)
        elif not shared:
            self.shared_gpu_note.set_visible(False)

    def collect(self):
        c = copy.deepcopy(self.cfg)
        c["name"] = self.name.get_text().strip()
        c["notes"] = self.notes.get_text()
        c["autostart"] = self.autostart.get_active()
        c["backend"] = util.combo_key(self.backend)
        c["cpus"] = int(self.cpus.get_value())
        c["cpu_affinity"] = util.combo_key(self.aff)
        c["cpu_set"] = self.cpuset.get_text().strip()
        c["memory_mib"] = int(self.mem.get_value())
        c["swiotlb_mib"] = int(self.swiotlb.get_value())
        c["hugepages"] = self.huge.get_active()
        c["disk_async"] = util.combo_key(self.aio)
        c["memory_mode"] = util.combo_key(self.memmode)
        b = c["boot"]
        b["mode"] = util.combo_key(self.bootmode)
        b["root"] = self.root.get_text().strip()
        b["rootfstype"] = self.rootfstype.get_text().strip()
        b["rootflags"] = self.rootflags.get_text().strip()
        b["cmdline_extra"] = self.cmdline.get_text().strip()
        b["initrd"] = self.initrd.get_active()
        b["firmware_debug"] = self.fwdebug.get_active()
        c["network"]["mode"] = util.combo_key(self.net)
        c["network"]["vhost_net"] = self.vhost.get_active()
        c["network"]["mac"] = self.mac.get_text().strip().lower()
        d = c["display"]
        d["gpu"] = util.combo_key(self.gpu)
        d["width"], d["height"], d["dpi"] = int(self.width.get_value()), int(self.height.get_value()), int(self.dpi.get_value())
        d["keyboard"], d["mouse"] = self.kbd.get_active(), self.mouse.get_active()
        d["keyboard_grab"] = self.kgrab.get_active() and d["keyboard"]
        d["autologin"], d["glamor"] = self.autologin.get_active(), self.glamor.get_active()
        d["session"] = util.combo_key(self.session)
        d["input_mode"] = util.combo_key(self.inmode)
        d["fullscreen"] = self.fullscreen.get_active()
        d["output"] = util.combo_key(self.output)
        d["fullscreen_native"] = self.fsnative.get_active()
        d["vnc"], d["vnc_lan"] = self.vnc.get_active(), self.vnclan.get_active()
        d["vnc_port"], d["vnc_password"] = int(self.vncport.get_value()), self.vncpwon.get_active()
        ns = native_size(c)
        if ns:
            d["width"], d["height"] = ns
        c["sound"]["mode"] = util.combo_key(self.sound)
        c["cloud_init"]["enabled"] = self.ci.get_active()
        c["cloud_init"]["user"] = self.ciuser.get_text().strip()
        c["guest_tools"] = self.tools.get_active()
        if self.usb_rows is not None:
            c["usb"] = [{"vendor": r.dev["vendor"], "product": r.dev["product"]} for r in self.usb_rows if r.get_active()]
        return c

    def apply(self):
        c = self.collect()
        try:
            config.validate(c, self.feats)
        except config.ConfigError as e:
            util.error(self.win, "Invalid settings", str(e))
            return
        if c["memory_mib"] + c["swiotlb_mib"] > host.max_guest_mib(0) + 512:
            util.error(self.win, "Too much memory", "Keep at least %d MiB for the tablet." % config.HOST_RESERVE_MIB)
            return
        # 0.2-7: the VNC password lives in the VM folder (0600), never in vm.json
        pwfile = os.path.join(config.vm_dir(self.win.home, c["id"]), config.VNC_PASSWORD_FILE)
        newpw = self.vncpw.get_text()
        if c["display"]["vnc_password"] and not newpw and not os.path.exists(pwfile):
            util.error(self.win, "Invalid settings", "Type a VNC password (or turn the password off).")
            return
        if newpw:
            fd = os.open(pwfile, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "w") as fh:
                fh.write(newpw.strip()[:8] + "\n")
            self.vncpw.set_text("")
        ci_changed = c["cloud_init"] != self.cfg["cloud_init"] or c["name"] != self.cfg["name"]
        storage.save_vm(self.win.home, c)
        storage.sync_autostart(self.win.home)
        self.cfg = c
        if c["cloud_init"]["enabled"] and ci_changed:
            from .. import seed
            util.in_thread(lambda: seed.write_seed(self.win.home, c), None)
        self.win.toast("Settings saved")
        self.on_saved(c)

    def revert(self):
        saved = next((v for v in storage.list_vms(self.win.home) if v["id"] == self.cfg["id"]), self.cfg)
        self.on_saved(saved)          # the details page rebuilds this editor from the saved file
