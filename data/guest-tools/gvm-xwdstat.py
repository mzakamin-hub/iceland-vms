#!/usr/bin/python3
"""gvm-xwdstat.py [DISPLAY] - (in the guest, as root) grab the X root window with xwd and print
GUEST-XWD w=.. h=.. mean=.. nonzero=..  (mean of the RGB channels 0..1, fraction of non-black pixels,
sampled every 4th pixel of every 4th row; 24 or 32 bpp). Used by gvm.sh v5 to tell a black guest desktop from a black host window."""
import glob
import os
import struct
import subprocess
import sys

disp = sys.argv[1] if len(sys.argv) > 1 else ":0"
env = dict(os.environ, DISPLAY=disp)
for a in ["/run/lightdm/root/" + disp, "/var/run/lightdm/root/" + disp] + glob.glob("/run/user/*/gdm/Xauthority") \
        + glob.glob(os.path.join("/", "home", "*", ".Xauthority")) + glob.glob("/run/sddm/xauth_*"):
    if os.path.exists(a):
        env["XAUTHORITY"] = a
        break
try:
    d = subprocess.run(["xwd", "-root", "-silent"], env=env, capture_output=True, timeout=30).stdout
except (OSError, subprocess.TimeoutExpired) as e:
    print("GUEST-XWD error %s" % e)
    sys.exit(1)
if len(d) < 100:
    print("GUEST-XWD error: xwd gave %d bytes (no X display %s?)" % (len(d), disp))
    sys.exit(1)
h = struct.unpack(">25I", d[:100])
hsize, w, ht, order, bpp, bpl, ncolors = h[0], h[4], h[5], h[7], h[11], h[12], h[19]
off = hsize + ncolors * 12
px = d[off:]
if bpp not in (24, 32):
    print("GUEST-XWD w=%d h=%d bpp=%d (only 24 / 32 bpp are measured)" % (w, ht, bpp))
    sys.exit(1)
bp = bpp // 8                     # 24 bpp xwd (depth 24 X server, 24 Dec): 3 bytes per pixel
tot = nz = 0
s = 0
for y in range(0, ht, 4):
    row = px[y * bpl:y * bpl + w * bp]
    for x in range(0, len(row) - bp + 1, 4 * bp):
        r, g, b = row[x], row[x + 1], row[x + 2]
        s += r + g + b
        nz += 1 if (r or g or b) else 0
        tot += 1
print("GUEST-XWD w=%d h=%d mean=%.3f nonzero=%.3f" % (w, ht, s / 765.0 / max(tot, 1), nz / max(tot, 1)))
