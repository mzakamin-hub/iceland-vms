"""Application and main window: VM list (sidebar) + the selected VM (content)."""
import os
import sys

from gi.repository import Adw, Gio, GLib, Gtk

from .. import APP_ID, VERSION, backend, host, storage
from . import util
from .details import DetailsPage
from .hostinfo import HostDialog
from .wizard import CreateWizard

STATE_ICON = {"running": "media-playback-start-symbolic", "starting": "content-loading-symbolic",
              "rebooting": "view-refresh-symbolic", "stopping": "content-loading-symbolic",
              "failed": "dialog-error-symbolic", "stopped": "media-playback-stop-symbolic"}


class VmRow(Gtk.ListBoxRow):
    def __init__(self, cfg):
        super().__init__()
        self.cfg = cfg
        box = Gtk.Box(spacing=12, margin_top=8, margin_bottom=8, margin_start=6, margin_end=6)
        self.icon = Gtk.Image.new_from_icon_name("computer-symbolic")
        self.icon.set_pixel_size(32)
        box.append(self.icon)
        v = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER, hexpand=True)
        self.title = Gtk.Label(xalign=0, ellipsize=3)
        self.title.add_css_class("heading")
        self.sub = Gtk.Label(xalign=0, ellipsize=3)
        self.sub.add_css_class("dim-label")
        self.sub.add_css_class("caption")
        v.append(self.title)
        v.append(self.sub)
        box.append(v)
        self.state_img = Gtk.Image()
        box.append(self.state_img)
        self.set_child(box)
        self.update(cfg, {"state": "stopped"})

    def update(self, cfg, st):
        self.cfg = cfg
        self.st = st
        self.title.set_label(cfg["name"])
        mem = cfg["memory_mib"] / 1024
        self.sub.set_label("%s - %d CPUs, %s GiB" % (backend.STATES.get(st["state"], st["state"]), cfg["cpus"],
                                                    ("%.1f" % mem).rstrip("0").rstrip(".")))
        self.state_img.set_from_icon_name(STATE_ICON.get(st["state"], "media-playback-stop-symbolic"))


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        w, h = (int(x) for x in os.environ.get("IVMS_SIZE", "1180x780").split("x"))   # IVMS_SIZE: screenshots
        super().__init__(application=app, title="Virtual Machines", default_width=w, default_height=h)
        self.set_size_request(360, 480)
        self.home = backend.home()
        storage.ensure_layout(self.home)
        self.rows = {}
        self.toasts = Adw.ToastOverlay()

        # sidebar
        sb_tv = Adw.ToolbarView()
        hb = Adw.HeaderBar()
        add = util.button(icon="list-add-symbolic", tooltip="New virtual machine", cb=self.create_vm)
        hb.pack_start(add)
        menu = Gio.Menu()
        menu.append("Host Status", "app.host")
        menu.append("Refresh", "app.refresh")
        menu.append("About Virtual Machines", "app.about")
        mb = Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu, tooltip_text="Main menu")
        hb.pack_end(mb)
        sb_tv.add_top_bar(hb)
        self.listbox = Gtk.ListBox()
        self.listbox.add_css_class("navigation-sidebar")
        self.listbox.connect("row-selected", self.on_selected)
        sc = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        sc.set_child(self.listbox)
        self.sidebar_stack = Gtk.Stack()
        self.sidebar_stack.add_named(sc, "list")
        empty = Adw.StatusPage(icon_name="computer-symbolic", title="No Virtual Machines",
                               description="Create one from the catalogue, an ISO or a disk image")
        b = util.button(label="New Virtual Machine", css=["suggested-action", "pill"], cb=self.create_vm)
        b.set_halign(Gtk.Align.CENTER)
        empty.set_child(b)
        self.sidebar_stack.add_named(empty, "empty")
        self.banner = Adw.Banner()
        bx = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        bx.append(self.banner)
        bx.append(self.sidebar_stack)
        sb_tv.set_content(bx)
        sidebar = Adw.NavigationPage(title="Virtual Machines", child=sb_tv)

        # content
        self.content_stack = Gtk.Stack()
        none = Adw.ToolbarView()
        none.add_top_bar(Adw.HeaderBar(show_title=False))
        none.set_content(Adw.StatusPage(icon_name="computer-symbolic", title="Virtual Machines",
                                        description="Protected virtual machines on the Gunyah hypervisor"))
        self.content_stack.add_named(none, "none")
        self.details = DetailsPage(self)
        self.content_stack.add_named(self.details, "vm")
        self.content = Adw.NavigationPage(title="Virtual Machine", child=self.content_stack)

        self.split = Adw.NavigationSplitView(sidebar=sidebar, content=self.content)
        self.split.set_min_sidebar_width(260)
        self.split.set_max_sidebar_width(340)
        self.toasts.set_child(self.split)
        self.set_content(self.toasts)
        bp = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 720sp"))
        bp.add_setter(self.split, "collapsed", True)
        self.add_breakpoint(bp)

        self.reload()
        GLib.timeout_add_seconds(2, self.poll)

    def toast(self, text):
        self.toasts.add_toast(Adw.Toast(title=text, timeout=4))

    # ---- list ----
    def reload(self, select=None):
        vms = storage.list_vms(self.home)
        cur = select or (self.details.cfg["id"] if self.details.cfg else None)
        while (r := self.listbox.get_row_at_index(0)) is not None:
            self.listbox.remove(r)
        self.rows = {}
        for cfg in vms:
            row = VmRow(cfg)
            self.rows[cfg["id"]] = row
            self.listbox.append(row)
        self.sidebar_stack.set_visible_child_name("list" if vms else "empty")
        if cur in self.rows:
            self.listbox.select_row(self.rows[cur])
        elif not vms:
            self.details.show(None)
            self.content_stack.set_visible_child_name("none")
        self.poll()
        self.update_banner()

    def update_banner(self):
        msgs = []
        if not host.gvm_installed():
            msgs.append("iceland-gvm is not installed: VMs cannot start")
        elif not host.modules_match():
            msgs.append("This kernel has no Gunyah modules: install kernel-iceland q90m or later")
        self.banner.set_title("; ".join(msgs))
        self.banner.set_revealed(bool(msgs))

    def poll(self):
        for vid, row in self.rows.items():
            row.update(row.cfg, backend.state(vid))
        self.details.refresh_state()
        return True

    def on_selected(self, _lb, row):
        if row is None:
            return
        self.details.show(row.cfg)
        self.content.set_title(row.cfg["name"])
        self.content_stack.set_visible_child_name("vm")
        self.split.set_show_content(True)

    def create_vm(self):
        CreateWizard(self).present(self)


class App(Adw.Application):
    def __init__(self, argv):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.argv = argv
        self.win = None
        for name, cb in (("host", self.on_host), ("about", self.on_about), ("refresh", lambda *_: self.win.reload()),
                         ("quit", lambda *_: self.quit())):
            a = Gio.SimpleAction.new(name, None)
            a.connect("activate", cb)
            self.add_action(a)
        self.set_accels_for_action("app.quit", ["<Control>q"])
        self.set_accels_for_action("app.refresh", ["F5"])

    def do_activate(self):
        if not self.win:
            self.win = MainWindow(self)
        self.win.present()
        if "--screenshots" in self.argv:
            from . import shots
            shots.run(self.win, self.argv[self.argv.index("--screenshots") + 1])
        elif "--autostart" in self.argv:
            self.win.details.autostart_all()

    def on_host(self, *_):
        HostDialog(self.win).present(self.win)

    def on_about(self, *_):
        a = Adw.AboutDialog(application_name="Virtual Machines", application_icon=APP_ID, version=VERSION,
                            developer_name="iceland project", license_type=Gtk.License.GPL_2_0,
                            comments="Protected virtual machines on the OnePlus Pad 4 (Qualcomm Gunyah hypervisor, crosvm)",
                            issue_url="")
        a.present(self.win)


def main():
    Adw.init()
    return App(sys.argv).run([sys.argv[0]])
