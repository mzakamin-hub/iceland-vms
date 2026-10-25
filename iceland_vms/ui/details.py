"""The selected VM: actions, overview, serial console, settings, snapshots, logs."""
import os
import time

from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from .. import backend, catalogue, config, host, seed, storage
from . import util
from .settings import SettingsPage

try:
    import gi
    gi.require_version("Vte", "3.91")
    from gi.repository import Vte
except (ValueError, ImportError):
    Vte = None


class ConsoleView(Gtk.Box):
    """Interactive serial console (VTE when installed, a plain text view otherwise)."""

    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.client = None
        self.vmid = None
        bar = Gtk.Box(spacing=6, margin_top=6, margin_bottom=6, margin_start=12, margin_end=12)
        self.status = Gtk.Label(xalign=0, hexpand=True, label="Not connected")
        self.status.add_css_class("dim-label")
        bar.append(self.status)
        self.btn = util.button(label="Connect", cb=self.toggle)
        bar.append(self.btn)
        self.append(bar)
        if Vte:
            self.term = Vte.Terminal(vexpand=True, hexpand=True)
            self.term.set_scrollback_lines(20000)
            self.term.connect("commit", lambda _t, text, _n: self.client and self.client.send(text.encode()))
            self.append(self.term)
        else:
            sc = Gtk.ScrolledWindow(vexpand=True)
            self.text = Gtk.TextView(editable=False, monospace=True, wrap_mode=Gtk.WrapMode.CHAR)
            sc.set_child(self.text)
            self.append(sc)
            self.entry = Gtk.Entry(placeholder_text="Type a line and press Enter", margin_start=12, margin_end=12,
                                   margin_bottom=12, margin_top=6)
            self.entry.connect("activate", self.on_entry)
            self.append(self.entry)

    def on_entry(self, e):
        if self.client:
            self.client.send((e.get_text() + "\r").encode())
        e.set_text("")

    def set_vm(self, vmid):
        if vmid != self.vmid:
            self.disconnect_()
            self.vmid = vmid
            if Vte:
                self.term.reset(True, True)
            else:
                self.text.get_buffer().set_text("")

    def feed(self, data):
        if Vte:
            self.term.feed(data)
        else:
            b = self.text.get_buffer()
            b.insert(b.get_end_iter(), data.decode("utf-8", "replace").replace("\r", ""))
            self.text.scroll_to_iter(b.get_end_iter(), 0, False, 0, 0)
        return False

    def toggle(self):
        if self.client:
            self.disconnect_()
        else:
            self.connect_()

    def connect_(self):
        if not self.vmid or self.client:
            return
        c = backend.ConsoleClient(self.vmid, lambda d: GLib.idle_add(self.feed, d),
                                  lambda: GLib.idle_add(self.closed))
        try:
            c.connect()
        except OSError as e:
            self.status.set_label("Console not available (%s)" % (e.strerror or e))
            return
        self.client = c
        self.status.set_label("Connected to the serial console (ttyS0)")
        self.btn.set_label("Disconnect")

    def closed(self):
        self.client = None
        self.status.set_label("Disconnected")
        self.btn.set_label("Connect")
        return False

    def disconnect_(self):
        if self.client:
            self.client.close()
        self.closed()


class DetailsPage(Adw.Bin):
    def __init__(self, win):
        super().__init__()
        self.win = win
        self.cfg = None
        self.st = {"state": "stopped"}
        tv = Adw.ToolbarView()
        hb = Adw.HeaderBar()
        self.stack = Adw.ViewStack()
        sw = Adw.ViewSwitcher(stack=self.stack, policy=Adw.ViewSwitcherPolicy.WIDE)
        hb.set_title_widget(sw)
        self.b_start = util.button(icon="media-playback-start-symbolic", tooltip="Start", css=["suggested-action"],
                                   cb=self.start)
        self.b_shutdown = util.button(icon="system-shutdown-symbolic", tooltip="Shut down", cb=self.shutdown)
        self.b_force = util.button(icon="process-stop-symbolic", tooltip="Force off", css=["destructive-action"],
                                   cb=self.force_off)
        for b in (self.b_start, self.b_shutdown, self.b_force):
            hb.pack_start(b)
        menu = Gio.Menu()
        menu.append("Clone...", "vm.clone")
        menu.append("Open Folder", "vm.folder")
        menu.append("Rewrite cloud-init Seed", "vm.seed")
        menu.append("Delete...", "vm.delete")
        ag = Gio.SimpleActionGroup()
        for name, cb in (("clone", self.clone), ("folder", self.open_folder), ("delete", self.delete),
                         ("seed", self.rewrite_seed)):
            a = Gio.SimpleAction.new(name, None)
            a.connect("activate", lambda *_a, f=cb: f())
            ag.add_action(a)
        self.insert_action_group("vm", ag)
        hb.pack_end(Gtk.MenuButton(icon_name="view-more-symbolic", menu_model=menu, tooltip_text="More"))
        tv.add_top_bar(hb)

        self.overview = Adw.PreferencesPage()
        self.stack.add_titled_with_icon(self.overview, "overview", "Overview", "computer-symbolic")
        self.console = ConsoleView()
        self.stack.add_titled_with_icon(self.console, "console", "Console", "utilities-terminal-symbolic")
        self.settings_holder = Adw.Bin()
        self.stack.add_titled_with_icon(self.settings_holder, "settings", "Settings", "emblem-system-symbolic")
        self.snap_page = Adw.PreferencesPage()
        self.stack.add_titled_with_icon(self.snap_page, "snapshots", "Snapshots", "camera-photo-symbolic")
        self.logs = self._logs_page()
        self.stack.add_titled_with_icon(self.logs, "logs", "Logs", "text-x-generic-symbolic")
        tv.set_content(self.stack)
        sb = Adw.ViewSwitcherBar(stack=self.stack)
        tv.add_bottom_bar(sb)
        bp_bin = Adw.BreakpointBin(width_request=300, height_request=300)
        bp_bin.set_child(tv)
        bp = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 640sp"))
        bp.add_setter(sw, "visible", False)
        bp.add_setter(sb, "reveal", True)
        bp_bin.add_breakpoint(bp)
        self.set_child(bp_bin)
        self.stack.connect("notify::visible-child-name", self.on_tab)

    # ---- selection / state ----
    def show(self, cfg):
        self.cfg = cfg
        if cfg is None:
            return
        self.console.set_vm(cfg["id"])
        self.settings = SettingsPage(self.win, cfg, self.on_saved)
        self.settings_holder.set_child(self.settings)
        self.st = backend.state(cfg["id"])
        self.build_overview()
        self.build_snapshots()
        self.refresh_buttons()

    def on_saved(self, cfg):
        self.cfg = cfg
        self.win.reload(select=cfg["id"])
        self.build_overview()

    def refresh_state(self):
        if not self.cfg:
            return
        st = backend.state(self.cfg["id"])
        changed = st != self.st
        self.st = st
        if changed:
            self.build_overview()
            self.refresh_buttons()
            if st["state"] == "running" and not self.console.client:
                self.console.connect_()

    def refresh_buttons(self):
        s = self.st["state"]
        self.b_start.set_visible(s in ("stopped", "failed"))
        self.b_shutdown.set_visible(s in ("running", "rebooting"))
        self.b_force.set_visible(s in ("running", "starting", "rebooting", "stopping"))

    def on_tab(self, *_):
        name = self.stack.get_visible_child_name()
        if name == "logs":
            self.load_log()
        elif name == "console" and self.st["state"] == "running":
            self.console.connect_()

    # ---- overview ----
    def build_overview(self):
        page = self.overview
        for g in getattr(self, "_ov_groups", []):
            page.remove(g)
        self._ov_groups = []
        c, st = self.cfg, self.st

        def group(title, desc=None):
            g = Adw.PreferencesGroup(title=title)
            if desc:
                g.set_description(desc)
            page.add(g)
            self._ov_groups.append(g)
            return g

        g = group(c["name"], c.get("notes") or None)
        r = Adw.ActionRow(title=backend.STATES.get(st["state"], st["state"]), subtitle=st.get("message") or "")
        r.add_prefix(Gtk.Image.new_from_icon_name(
            {"running": "emblem-ok-symbolic", "failed": "dialog-error-symbolic"}.get(st["state"], "computer-symbolic")))
        g.add(r)
        if st.get("ip"):
            r = Adw.ActionRow(title="Guest address", subtitle=st["ip"])
            r.add_suffix(util.button(icon="edit-copy-symbolic", tooltip="Copy", css=["flat"],
                                     cb=lambda ip=st["ip"]: self.copy(ip)))
            g.add(r)
            if c["cloud_init"]["enabled"]:
                cmd = "ssh %s@%s" % (c["cloud_init"]["user"], st["ip"])
                r = Adw.ActionRow(title="SSH", subtitle=cmd)
                r.add_suffix(util.button(icon="edit-copy-symbolic", tooltip="Copy", css=["flat"],
                                         cb=lambda: self.copy(cmd)))
                g.add(r)
        if c["cloud_init"]["enabled"]:
            r = Adw.ActionRow(title="Guest user", subtitle="%s (password hidden)" % c["cloud_init"]["user"])
            r.add_suffix(util.button(icon="edit-copy-symbolic", tooltip="Copy the password", css=["flat"],
                                     cb=self.copy_password))
            g.add(r)
        if c["display"]["gpu"] != "none" and st["state"] == "running":
            g.add(util.info_row("Display", "The guest screen is a separate window on this desktop; keyboard and "
                                "mouse work inside it while it has focus.", "video-display-symbolic"))

        g = group("Resources")
        ent = catalogue.get(c["os"]) if c["os"] != "custom" else None
        rows = [
            ("System", ent["name"] if ent else "Custom"),
            ("Boot", "Tablet guest kernel (direct)" if c["boot"]["mode"] == "direct" else "UEFI firmware"),
            ("Processors", "%d vCPUs%s" % (c["cpus"], "" if c["cpu_affinity"] == "any" else " on %s cores" % c["cpu_affinity"])),
            ("Memory", "%s GiB + %d MiB swiotlb" % (("%.2f" % (c["memory_mib"] / 1024)).rstrip("0").rstrip("."), c["swiotlb_mib"])),
            ("Storage", ", ".join("%s%s" % (d["name"], " (ro)" if d["ro"] else "") for d in c["disks"]) or "none"),
            ("Network", {"off": "Off", "nat": "NAT (shared through the tablet's connection)"}[c["network"]["mode"]]),
            ("Display", {"none": "None (console only)", "2d": "2D window", "virgl": "3D OpenGL (virgl)", "native": "3D native context",
                         "venus": "3D OpenGL (virgl) + Vulkan (venus)",
                         "simplefb": "Software display (shared framebuffer)"}[c["display"]["gpu"]]),
            ("Disk usage", util.human_size(storage.usage(self.win.home, c))),
        ]
        for t, s in rows:
            r = Adw.ActionRow(title=t, subtitle=s)
            r.add_css_class("property")
            g.add(r)

        if st["state"] in ("stopped", "failed"):
            why = host.start_guard(c, running=backend.running_count())
            if why:
                g = group("Cannot start now")
                for w in why:
                    g.add(util.info_row(w, "", "dialog-warning-symbolic"))
        warn = host.start_warnings(c)
        if warn:
            g = group("Before you start")
            for w in warn:
                g.add(util.info_row(w, "", "dialog-warning-symbolic"))
        untested = []
        if c["boot"]["mode"] == "uefi" or (ent and ent.get("status") != "proven"):
            untested.append(("Untested combination", "This system or boot mode has been prepared on the PC but "
                             "not yet run on the tablet. Report what happens (Logs tab)."))
        if c["memory_mib"] > host.features()["max_proven_memory_mib"]:
            untested.append(("Larger than tested", "%d MiB of guest memory has not been run on this tablet yet."
                             % c["memory_mib"]))
        if untested:
            g = group("Not yet tested on the tablet")
            for t, s in untested:
                g.add(util.info_row(t, s, "dialog-warning-symbolic"))

    def copy(self, text):
        Gdk.Display.get_default().get_clipboard().set(text)
        self.win.toast("Copied")

    def copy_password(self):
        try:
            self.copy(seed.password(self.win.home, self.cfg))
        except OSError as e:
            util.error(self.win, "No password", str(e))

    # ---- actions ----
    def start(self):
        c = self.cfg
        try:
            config.validate(c, host.features())
        except config.ConfigError as e:
            util.error(self.win, "Cannot start", str(e))
            return
        why = host.start_guard(c, running=backend.running_count())
        if why:
            util.error(self.win, "Cannot start", "\n".join(why))
            return
        if c["cloud_init"]["enabled"] and not os.path.exists(os.path.join(config.vm_dir(self.win.home, c["id"]),
                                                                          c["cloud_init"]["seed"])):
            try:
                seed.write_seed(self.win.home, c)
            except Exception as e:
                util.error(self.win, "Cannot write the cloud-init seed", str(e))
                return
        self.st = {"state": "starting", "message": "Waiting for authorization"}
        self.build_overview()
        self.refresh_buttons()

        def done(ok, out, err):
            if not ok:
                util.error(self.win, "Could not start %s" % c["name"], err or out)
            else:
                self.win.toast("%s started" % c["name"])
            self.win.poll()
        util.run_async(backend.helper_argv("start", c["id"]), done)

    def shutdown(self):
        c, ip = self.cfg, self.st.get("ip")

        def via_agent():
            util.run_async(backend.helper_argv("shutdown", c["id"]),
                           lambda ok, out, err: self.win.toast("Shutting down") if ok else
                           util.error(self.win, "Shut down failed", (err or out) + "\n\nUse Force Off if the guest does not react."))
        if self.st.get("agent"):
            via_agent()                 # guest agent on hvc0 (cloud-init installs it)
        elif ip and c["cloud_init"]["enabled"]:
            util.in_thread(lambda: backend.guest_poweroff(c, ip),
                           lambda r, e: self.win.toast("Shutting down") if r else via_agent())
        else:
            via_agent()

    def force_off(self):
        def go(resp):
            if resp == "off":
                util.run_async(backend.helper_argv("force-off", self.cfg["id"]),
                               lambda ok, out, err: ok or util.error(self.win, "Force off failed", err or out))
        util.alert(self.win, "Force off %s?" % self.cfg["name"],
                   "This is like pulling the power plug: unsaved data in the guest is lost.",
                   (("cancel", "Cancel"), ("off", "Force Off")), default="cancel", destructive="off", callback=go)

    def clone(self):
        if self.st["state"] not in ("stopped", "failed"):
            util.error(self.win, "Cannot clone", "Stop the virtual machine first.")
            return
        d = Adw.AlertDialog.new("Clone %s" % self.cfg["name"], "Disks are copied (instantly on btrfs).")
        e = Gtk.Entry(text="%s (copy)" % self.cfg["name"][:56])
        d.set_extra_child(e)
        d.add_response("cancel", "Cancel")
        d.add_response("clone", "Clone")
        d.set_response_appearance("clone", Adw.ResponseAppearance.SUGGESTED)

        def resp(_d, r):
            if r != "clone":
                return
            name = e.get_text().strip()[:64] or "Clone"
            util.in_thread(lambda: storage.clone_vm(self.win.home, self.cfg, name),
                           lambda new, err: util.error(self.win, "Clone failed", err) if err
                           else self.win.reload(select=new["id"]))
        d.connect("response", resp)
        d.present(self.win)

    def delete(self):
        if self.st["state"] not in ("stopped", "failed"):
            util.error(self.win, "Cannot delete", "Stop the virtual machine first.")
            return
        size = util.human_size(storage.usage(self.win.home, self.cfg))

        def go(resp):
            if resp == "delete":
                storage.delete_vm(self.win.home, self.cfg)
                self.cfg = None
                self.win.reload()
                self.win.toast("Deleted")
        util.alert(self.win, "Delete %s?" % self.cfg["name"],
                   "Its disks, snapshots and settings (%s) are deleted permanently. Downloaded ISOs and images "
                   "stay in the shared folders." % size,
                   (("cancel", "Cancel"), ("delete", "Delete")), default="cancel", destructive="delete", callback=go)

    def open_folder(self):
        Gio.AppInfo.launch_default_for_uri(
            Gio.File.new_for_path(config.vm_dir(self.win.home, self.cfg["id"])).get_uri(), None)

    def rewrite_seed(self):
        if not self.cfg["cloud_init"]["enabled"]:
            util.error(self.win, "cloud-init is off", "Enable cloud-init in Settings first.")
            return
        util.in_thread(lambda: seed.write_seed(self.win.home, self.cfg),
                       lambda r, e: util.error(self.win, "Seed failed", e) if e else
                       self.win.toast("Seed rewritten: applied at the next boot"))

    def autostart_all(self):
        for cfg in storage.list_vms(self.win.home):
            if cfg["autostart"] and backend.state(cfg["id"])["state"] == "stopped":
                self.win.listbox.select_row(self.win.rows[cfg["id"]])
                self.start()
                break          # one VM at a time

    # ---- snapshots ----
    def build_snapshots(self):
        page = self.snap_page
        for g in getattr(self, "_sn_groups", []):
            page.remove(g)
        self._sn_groups = []
        g = Adw.PreferencesGroup(title="Snapshots",
                                 description="Offline copies of the VM's writable disks and settings. The VM must be "
                                 "stopped (a protected VM's memory cannot be saved: the host cannot read it).")
        g.set_header_suffix(util.button(icon="list-add-symbolic", tooltip="Take snapshot", css=["flat"],
                                        cb=self.take_snapshot))
        page.add(g)
        self._sn_groups.append(g)
        snaps = storage.snapshots(self.win.home, self.cfg)
        if not snaps:
            g.add(Adw.ActionRow(title="No snapshots"))
        for s in snaps:
            r = Adw.ActionRow(title=s["label"] or s["dir"],
                              subtitle=time.strftime("%Y-%m-%d %H:%M", time.localtime(s["time"])))
            r.add_suffix(util.button(icon="edit-undo-symbolic", tooltip="Restore", css=["flat"],
                                     cb=lambda n=s["dir"]: self.restore_snapshot(n)))
            r.add_suffix(util.button(icon="user-trash-symbolic", tooltip="Delete", css=["flat"],
                                     cb=lambda n=s["dir"]: self.delete_snapshot(n)))
            g.add(r)

    def _need_stopped(self):
        if self.st["state"] not in ("stopped", "failed"):
            util.error(self.win, "The VM is running", "Shut it down first.")
            return False
        return True

    def take_snapshot(self):
        if not self._need_stopped():
            return
        label = time.strftime("Snapshot %Y-%m-%d %H:%M")
        util.in_thread(lambda: storage.take_snapshot(self.win.home, self.cfg, label),
                       lambda r, e: util.error(self.win, "Snapshot failed", e) if e else self.build_snapshots())

    def restore_snapshot(self, name):
        if not self._need_stopped():
            return

        def go(resp):
            if resp == "restore":
                util.in_thread(lambda: storage.restore_snapshot(self.win.home, self.cfg, name),
                               lambda r, e: util.error(self.win, "Restore failed", e) if e
                               else self.win.reload(select=self.cfg["id"]))
        util.alert(self.win, "Restore this snapshot?", "The current disks and settings are replaced.",
                   (("cancel", "Cancel"), ("restore", "Restore")), default="cancel", destructive="restore", callback=go)

    def delete_snapshot(self, name):
        def go(resp):
            if resp == "delete":
                storage.delete_snapshot(self.win.home, self.cfg, name)
                self.build_snapshots()
        util.alert(self.win, "Delete this snapshot?", "", (("cancel", "Cancel"), ("delete", "Delete")),
                   default="cancel", destructive="delete", callback=go)

    # ---- logs ----
    def _logs_page(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        bar = Gtk.Box(spacing=6, margin_top=6, margin_bottom=6, margin_start=12, margin_end=12)
        self.log_kind = Gtk.DropDown.new_from_strings(["Console", "crosvm", "Supervisor"])
        self.log_kind.connect("notify::selected", lambda *_: self.load_log())
        bar.append(self.log_kind)
        bar.append(Gtk.Box(hexpand=True))
        bar.append(util.button(icon="view-refresh-symbolic", tooltip="Reload", cb=self.load_log))
        box.append(bar)
        sc = Gtk.ScrolledWindow(vexpand=True)
        self.log_view = Gtk.TextView(editable=False, monospace=True, wrap_mode=Gtk.WrapMode.CHAR,
                                     left_margin=12, right_margin=12)
        sc.set_child(self.log_view)
        box.append(sc)
        return box

    def load_log(self):
        if not self.cfg:
            return
        kind = ["Console", "crosvm", "Supervisor"][self.log_kind.get_selected()]
        p = backend.log_paths(self.cfg["id"])[kind]
        text = backend.read_tail(p) if os.path.exists(p) else "No %s log yet (logs live in %s until the next reboot)." % (
            kind.lower(), os.path.dirname(p))
        self.log_view.get_buffer().set_text(text)
