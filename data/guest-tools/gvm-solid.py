#!/usr/bin/python3
# SPDX-License-Identifier: GPL-2.0-only
"""gvm-solid.py DISPLAY SECONDS - (in the guest) cover the whole X screen with one solid magenta window for SECONDS.

iceland gvm, vm-run v10e: the window opacity check. The host takes a screenshot of the VM window meanwhile; an opaque
window shows pure magenta, a see-through one (alpha used by the host compositor) shows the host wallpaper mixed in.
libX11 through ctypes only (no devel packages, no toolkit).
"""
import ctypes
import ctypes.util
import sys
import time

import glob
import os

disp_name = (sys.argv[1] if len(sys.argv) > 1 else ":0").encode()
for a in ["/run/lightdm/root/" + disp_name.decode(), "/var/run/lightdm/root/" + disp_name.decode()] \
        + glob.glob("/run/user/*/gdm/Xauthority") + glob.glob(os.path.join("/", "home", "*", ".Xauthority")) + glob.glob("/run/sddm/xauth_*"):
    if os.path.exists(a):          # as root, like gvm-xwdstat.py: the display manager's auth file
        os.environ["XAUTHORITY"] = a
        break
secs = float(sys.argv[2]) if len(sys.argv) > 2 else 20
x = ctypes.CDLL(ctypes.util.find_library("X11") or "libX11.so.6")
x.XOpenDisplay.restype = ctypes.c_void_p
x.XOpenDisplay.argtypes = [ctypes.c_char_p]
x.XDefaultRootWindow.restype = ctypes.c_ulong
x.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
x.XDisplayWidth.argtypes = x.XDisplayHeight.argtypes = [ctypes.c_void_p, ctypes.c_int]
x.XCreateSimpleWindow.restype = ctypes.c_ulong
x.XCreateSimpleWindow.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_int, ctypes.c_uint,
                                  ctypes.c_uint, ctypes.c_uint, ctypes.c_ulong, ctypes.c_ulong]


class XSetWindowAttributes(ctypes.Structure):
    _fields_ = [("background_pixmap", ctypes.c_ulong), ("background_pixel", ctypes.c_ulong),
                ("border_pixmap", ctypes.c_ulong), ("border_pixel", ctypes.c_ulong),
                ("bit_gravity", ctypes.c_int), ("win_gravity", ctypes.c_int), ("backing_store", ctypes.c_int),
                ("backing_planes", ctypes.c_ulong), ("backing_pixel", ctypes.c_ulong), ("save_under", ctypes.c_int),
                ("event_mask", ctypes.c_long), ("do_not_propagate_mask", ctypes.c_long),
                ("override_redirect", ctypes.c_int), ("colormap", ctypes.c_ulong), ("cursor", ctypes.c_ulong)]


x.XChangeWindowAttributes.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong,
                                      ctypes.POINTER(XSetWindowAttributes)]
for f in ("XMapRaised", "XClearWindow", "XDestroyWindow"):
    getattr(x, f).argtypes = [ctypes.c_void_p, ctypes.c_ulong]
x.XSync.argtypes = [ctypes.c_void_p, ctypes.c_int]

d = x.XOpenDisplay(disp_name)
if not d:
    print("GUEST-SOLID error: cannot open display %s" % disp_name.decode())
    sys.exit(1)
root = x.XDefaultRootWindow(d)
w, h = x.XDisplayWidth(d, 0), x.XDisplayHeight(d, 0)
win = x.XCreateSimpleWindow(d, root, 0, 0, w, h, 0, 0, 0xff00ff)
a = XSetWindowAttributes()
a.override_redirect = 1
x.XChangeWindowAttributes(d, win, 1 << 9, ctypes.byref(a))      # CWOverrideRedirect
x.XMapRaised(d, win)
x.XClearWindow(d, win)
x.XSync(d, 0)
print("GUEST-SOLID magenta %dx%d for %.0f s" % (w, h, secs), flush=True)
time.sleep(secs)
x.XDestroyWindow(d, win)
x.XSync(d, 0)
