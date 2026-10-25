"""Small GTK helpers shared by the windows."""
import threading

from gi.repository import Adw, Gio, GLib, Gtk


def human_size(n):
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024 or unit == "TiB":
            return ("%.0f %s" if unit == "B" else "%.1f %s") % (n, unit)
        n /= 1024.0


def run_async(argv, done):
    """Run argv without blocking the UI; done(ok, stdout, stderr) on the main loop."""
    try:
        p = Gio.Subprocess.new(argv, Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_PIPE)
    except GLib.Error as e:
        GLib.idle_add(done, False, "", e.message)
        return

    def fin(proc, res):
        try:
            _ok, out, err = proc.communicate_utf8_finish(res)
        except GLib.Error as e:
            done(False, "", e.message)
            return
        done(proc.get_exit_status() == 0, out or "", err or "")
    p.communicate_utf8_async(None, None, fin)


def in_thread(work, done=None):
    """work() in a thread; done(result, error) on the main loop."""
    def t():
        try:
            r, e = work(), None
        except Exception as ex:  # shown to the user
            r, e = None, str(ex) or ex.__class__.__name__
        if done:
            GLib.idle_add(done, r, e)
    threading.Thread(target=t, daemon=True).start()


def alert(parent, heading, body, responses=(("ok", "OK"),), default="ok", destructive=None, callback=None):
    d = Adw.AlertDialog.new(heading, body)
    for rid, label in responses:
        d.add_response(rid, label)
    d.set_default_response(default)
    d.set_close_response(responses[0][0])
    if destructive:
        d.set_response_appearance(destructive, Adw.ResponseAppearance.DESTRUCTIVE)
    if callback:
        d.connect("response", lambda _d, r: callback(r))
    d.present(parent)
    return d


def error(parent, heading, text):
    msg = (text or "").strip()
    if msg.startswith("error: "):
        msg = msg[7:]
    if "Not authorized" in msg or "dismissed" in msg.lower():
        msg = "The administrator password was not given."
    alert(parent, heading, msg[-1500:] or "Unknown error")


def spin_row(title, subtitle, lo, hi, step, value):
    adj = Gtk.Adjustment(lower=lo, upper=hi, step_increment=step, page_increment=step * 4, value=value)
    r = Adw.SpinRow(title=title, adjustment=adj)
    if subtitle:
        r.set_subtitle(subtitle)
    return r


def combo_row(title, items, selected=0, subtitle=None):
    r = Adw.ComboRow(title=title, model=Gtk.StringList.new([label for _k, label in items]))
    r.set_selected(selected)
    if subtitle:
        r.set_subtitle(subtitle)
    r.keys = [k for k, _label in items]
    return r


def combo_key(row):
    return row.keys[row.get_selected()]


def combo_set(row, key):
    if key in row.keys:
        row.set_selected(row.keys.index(key))


def info_row(title, subtitle, icon="dialog-information-symbolic"):
    r = Adw.ActionRow(title=title, subtitle=subtitle)
    r.set_subtitle_lines(0)
    r.add_prefix(Gtk.Image.new_from_icon_name(icon))
    return r


def button(label=None, icon=None, css=None, tooltip=None, cb=None):
    b = Gtk.Button()
    if icon and label:
        b.set_child(Adw.ButtonContent(icon_name=icon, label=label))
    elif icon:
        b.set_icon_name(icon)
    else:
        b.set_label(label)
    for c in (css or []):
        b.add_css_class(c)
    if tooltip:
        b.set_tooltip_text(tooltip)
    if cb:
        b.connect("clicked", lambda *_: cb())
    b.set_valign(Gtk.Align.CENTER)
    return b
