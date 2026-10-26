#!/usr/bin/python3
# SPDX-License-Identifier: GPL-2.0-only
"""gvm-evrec.py SECONDS [NAME-REGEX] [kbd] - guest input event recorder for the VM window input rows (VIN, 0.2-6).

Run as root in the guest. Opens every /dev/input/event* whose name matches NAME-REGEX (default: crosvm's and
gvm's virtio-input devices, keyboards excluded unless "kbd" is given: V9-3), without grabbing them (the desktop keeps getting the events),
and prints JSON lines: {"dev": name, "path": p} per device, then "READY", then {"d": name, "t": seconds,
"ty": type, "c": code, "v": value} per event (EV_SYN left out) until SECONDS have passed. Like evtest, but
non-interactive and for several devices at once. No dependencies beyond python3."""
import fcntl
import glob
import json
import os
import re
import select
import struct
import sys
import time

EVIOCGNAME = lambda n: (2 << 30) | (n << 16) | (ord("E") << 8) | 0x06  # noqa: E731
FMT = "qqHHi"
SZ = struct.calcsize(FMT)


def name_of(fd):
    buf = bytearray(256)
    try:
        n = fcntl.ioctl(fd, EVIOCGNAME(len(buf)), buf, True)
    except OSError:
        return ""
    return bytes(buf[:max(0, n - 1)]).decode("utf-8", "replace")


def main(argv):
    secs = float(argv[1]) if len(argv) > 1 else 8.0
    pat = re.compile(argv[2] if len(argv) > 2 and argv[2] else r"(?i)crosvm|gvm")
    kbd = len(argv) > 3 and argv[3] == "kbd"
    fds = {}
    for p in sorted(glob.glob("/dev/input/event*")):
        try:
            fd = os.open(p, os.O_RDONLY | os.O_NONBLOCK)
        except OSError:
            continue
        n = name_of(fd)
        if not pat.search(n) or ("keyboard" in n.lower() and not kbd):
            os.close(fd)
            continue
        fds[fd] = n
        print(json.dumps({"dev": n, "path": p}))
        # p3: the current absolute axes (evdev sends a value only when it changes, so a gesture at the same x as
        # the previous one carries no ABS_X: the judge starts from these)
        bits = bytearray(8)
        try:      # EVIOCGBIT(EV_ABS): the device's absolute axes
            fcntl.ioctl(fd, (2 << 30) | (len(bits) << 16) | (ord("E") << 8) | (0x20 + 3), bits, True)
        except OSError:
            bits = bytearray(8)
        for code in (0x00, 0x01, 0x2f, 0x35, 0x36):
            if not bits[code // 8] & (1 << (code % 8)):
                continue
            buf = bytearray(24)
            try:
                fcntl.ioctl(fd, (2 << 30) | (24 << 16) | (ord("E") << 8) | (0x40 + code), buf, True)
            except OSError:
                continue
            print(json.dumps({"init": n, "c": code, "v": struct.unpack_from("i", buf, 0)[0]}))
    print("READY %d devices" % len(fds), flush=True)
    t0 = time.monotonic()
    end = t0 + secs
    while True:
        left = end - time.monotonic()
        if left <= 0:
            break
        r, _, _ = select.select(list(fds), [], [], left)
        for fd in r:
            try:
                data = os.read(fd, SZ * 64)
            except BlockingIOError:
                continue
            for i in range(0, len(data) - SZ + 1, SZ):
                s, us, ty, c, v = struct.unpack_from(FMT, data, i)
                if ty == 0:
                    continue
                print(json.dumps({"d": fds[fd], "t": round(s + us / 1e6, 4), "ty": ty, "c": c, "v": v}))
        sys.stdout.flush()
    print("END", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
