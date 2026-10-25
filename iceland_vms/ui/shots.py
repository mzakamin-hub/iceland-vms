"""--screenshots DIR: render the main views to PNG files (documentation; works under WSLg / any backend)."""
import os

from gi.repository import GLib, Graphene, Gtk


def save(widget, path):
    """Render widget to path; False when it has not been drawn yet (the caller retries)."""
    w, h = widget.get_width(), widget.get_height()
    if w <= 0 or h <= 0:
        return False
    snap = Gtk.Snapshot()
    Gtk.WidgetPaintable.new(widget).snapshot(snap, w, h)
    node = snap.to_node()
    if node is None:
        return False
    tex = widget.get_native().get_renderer().render_texture(node, Graphene.Rect().init(0, 0, w, h))
    tex.save_to_png(path)
    print("wrote", path)
    return True


def run(win, outdir):
    os.makedirs(outdir, exist_ok=True)
    holder = {}
    steps = []

    def act(fn, delay=900):
        steps.append(("act", fn, delay))

    def shot(name, target=None):
        steps.append(("shot", (name, target), 700))

    def select_first():
        r = win.listbox.get_row_at_index(0)
        if r:
            win.listbox.select_row(r)

    def open_wizard():
        from .wizard import CreateWizard
        holder["w"] = CreateWizard(win)
        holder["w"].present(win)

    def push_cat():
        holder["w"].nav.push(holder["w"]._catalogue_page())

    def push_res():
        from .. import catalogue
        es = catalogue.load()
        holder["w"]._go_resources({"kind": "catalogue", "entry": es[0]} if es else {"kind": "test"})

    def host_dlg():
        from .hostinfo import HostDialog
        holder["h"] = HostDialog(win)
        holder["h"].present(win)

    act(select_first)
    shot("01-overview.png")
    act(lambda: win.details.stack.set_visible_child_name("settings"))
    shot("02-settings.png")
    act(lambda: win.details.stack.set_visible_child_name("snapshots"))
    shot("03-snapshots.png")
    act(lambda: win.details.stack.set_visible_child_name("console"))
    shot("04-console.png")
    act(lambda: win.details.stack.set_visible_child_name("overview"))
    act(open_wizard, 2500)
    shot("05-new-vm.png", lambda: holder["w"])
    act(push_cat, 1500)
    shot("06-catalogue.png", lambda: holder["w"])
    act(push_res, 1500)
    shot("07-resources.png", lambda: holder["w"])
    act(lambda: holder["w"].close())
    act(host_dlg, 3000)
    shot("08-host-status.png", lambda: holder["h"])
    act(lambda: holder["h"].close())
    act(lambda: win.set_default_size(420, 860), 2000)
    shot("09-narrow.png")
    act(lambda: win.get_application().quit())

    def step(i=0, tries=0):
        if i >= len(steps):
            return False
        kind, arg, delay = steps[i]
        try:
            if kind == "act":
                arg()
            else:
                name, target = arg
                if not save(target() if target else win, os.path.join(outdir, name)):
                    if tries < 8:
                        GLib.timeout_add(400, step, i, tries + 1)
                        return False
                    print("skipped", name)
        except Exception as e:  # best effort
            print("screenshot step %d failed: %s" % (i, e))
        GLib.timeout_add(delay, step, i + 1, 0)
        return False
    GLib.timeout_add(1500, step)
