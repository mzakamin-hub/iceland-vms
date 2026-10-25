#!/usr/bin/python3 -I
"""Headless ISO row through the VM app's OWN create path (U1 Ubuntu 26.04.1 server, U2 NixOS 26.05 minimal).

Run as the desktop user (not root):   python3 -I /usr/share/iceland-vms/tests/vm-iso-test.py U1
  rows: U1 = ubuntu-26.04-iso, U2 = nixos-26.05-iso, or --entry <catalogue id>
  --keep-vm      leave the VM (default: delete it)
  --delete-iso   delete the downloaded ISO too (default: keep it; the download is the slow part)
  --net          NAT on (default off: fewer side effects)  --timeout S  boot watch time (default 600)
  --lib DIR      iceland_vms package dir (default /usr/share/iceland-vms/lib)

What it does (the same calls as the wizard + the app's Start / Force Off buttons):
  1. create.create_vm(kind=catalogue): download, sha256 verify, UEFI VM, ISO as cdrom, 32 GiB disk
  2. sudo env PKEXEC_UID=<uid> iceland-vms-helper start <id>      (polkit's pkexec is interactive)
  3. watches console.log; on the GRUB menu it opens the entry editor over the serial console and appends
     console=ttyS0,115200 to the linux line (ISO kernels default to the DT console otherwise)
  4. sudo ... helper stop <id>; deletes the VM (and the ISO with --delete-iso)
Rows (PASS / FAIL / NOT-RUN), printed as "ROW <id> <result> <text>" and in ~/vm-iso-test-<row>.txt:
  x-1 create (download + sha256 + VM files)          x-2 helper start, unit running
  x-3 UEFI firmware output on the serial console     x-4 GRUB menu on the serial console
  x-5 Linux started (EFI stub / "Booting Linux" / "Linux version")
  x-6 restricted DMA pool used ("restricted DMA pool" / "restricted-dma-pool" in the kernel log)
  x-7 installer userspace (U1: casper / subiquity / "Ubuntu 26.04"; U2: "NixOS Stage 1" / "Welcome to NixOS"
      / "nixos login")                               x-8 stop + cleanup, unit gone
x-6 / x-7 need the kernel log on ttyS0 (step 3); if the GRUB edit misses, they are reported FAIL with
the reason "no kernel console" and x-5 still tells whether the kernel started.
"""
import argparse
import os
import re
import socket
import subprocess
import sys
import time

ROWS = {"U1": "ubuntu-26.04-iso", "U2": "nixos-26.05-iso"}
USERSPACE = {
    "ubuntu": [r"casper", r"subiquity", r"Ubuntu 26\.04", r"cloud-init"],
    "nixos": [r"NixOS Stage 1", r"Welcome to NixOS", r"nixos login", r"<<< NixOS"],
}
ANSI = re.compile(rb"\x1b\[[0-9;?]*[A-Za-z]|\x1b[()][A-Z0-9]|\x1b[=>]")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("row", nargs="?", default="U1")
    ap.add_argument("--entry")
    ap.add_argument("--keep-vm", action="store_true")
    ap.add_argument("--delete-iso", action="store_true")
    ap.add_argument("--net", action="store_true")
    ap.add_argument("--timeout", type=int, default=600)
    ap.add_argument("--mem", type=int, default=0, help="guest MiB (default: from MemAvailable, 2048..4096)")
    ap.add_argument("--grub-edit", action="store_true",
                    help="append console=ttyS0 in the GRUB editor (v4 run: the guessed line broke the boot; crosvm's DT "
                         "stdout-path already sends the kernel log to ttyS0, so the default is no edit)")
    ap.add_argument("--lib", default="/usr/share/iceland-vms/lib")
    a = ap.parse_args()
    if os.geteuid() == 0:
        sys.exit("run as the desktop user, not root (the helper is called through sudo)")
    sys.path.insert(0, a.lib)
    from iceland_vms import backend, catalogue, config, create, storage   # noqa: E402

    row = a.row
    eid = a.entry or ROWS.get(row)
    if not eid:
        sys.exit("unknown row %s (U1, U2 or --entry ID)" % row)
    home = os.path.expanduser("~")
    uid = os.getuid()
    out = open(os.path.join(home, "vm-iso-test-%s.txt" % row), "w")
    res = {}

    def say(*m):
        line = " ".join(str(x) for x in m)
        print(line, flush=True)
        out.write(line + "\n")
        out.flush()

    def rowres(n, ok, text):
        k = "%s-%d" % (row, n)
        res[k] = ok
        say("ROW %s %s %s" % (k, {True: "PASS", False: "FAIL", None: "NOT-RUN"}[ok], text))

    def helper(verb, vmid):
        r = subprocess.run(["sudo", "-n", "env", "PKEXEC_UID=%d" % uid, config.HELPER, verb, vmid],
                           capture_output=True, text=True, timeout=180)
        say("helper %s: rc=%d %s" % (verb, r.returncode, (r.stdout + r.stderr).strip()[-400:]))
        return r.returncode == 0

    entry = catalogue.get(eid)
    if not entry:
        sys.exit("catalogue entry %s missing" % eid)
    distro = "nixos" if "nixos" in eid else "ubuntu"
    rec = entry.get("recommended", {})
    # the helper's guard wants guest + swiotlb + 3072 MiB for the host free (run 2: 7221 < 7296 for 4096):
    # size the guest from MemAvailable, 2048..4096 MiB in 256 MiB steps
    avail = 0
    with open("/proc/meminfo") as f:
        for ln in f:
            if ln.startswith("MemAvailable:"):
                avail = int(ln.split()[1]) // 1024
    mem = min(rec.get("memory_mib", 4096), 4096, max(2048, (avail - 128 - 3072 - 256) // 256 * 256))
    if a.mem:
        mem = a.mem
    v = dict(name="%s test" % row, cpus=min(4, rec.get("cpus", 4)), mem=mem,
             disk=rec.get("disk_gib", 32), net=a.net, gpu="none")
    say("guest memory %d MiB (MemAvailable %d MiB)" % (mem, avail))
    say("%s: %s (%s) %s" % (row, entry["name"], eid, time.strftime("%F %T")))
    last = [0.0]

    def dl(stage, done, total):
        if time.time() - last[0] > 30 or done == total:
            last[0] = time.time()
            say("  %s %d / %d MiB" % (stage, done >> 20, (total or 0) >> 20))

    cfg = None
    try:
        t0 = time.time()
        cfg = create.create_vm(home, {"kind": "catalogue", "entry": entry}, v, dl_progress=dl)
        iso = storage.disk_path(home, cfg, cfg["disks"][0])
        rowres(1, True, "VM %s created in %d s: ISO %s sha256 verified, disk %d GiB, boot %s"
               % (cfg["id"], time.time() - t0, os.path.basename(iso), v["disk"], cfg["boot"]["mode"]))
    except Exception as e:                     # noqa: BLE001
        rowres(1, False, "create: %s" % e)
        for n in range(2, 9):
            rowres(n, None, "create failed")
        return finish(res, say)

    vmid = cfg["id"]
    logp = backend.log_paths(vmid)["Console"]
    started = helper("start", vmid)
    time.sleep(3)
    st = backend.state(vmid)
    rowres(2, started and st["state"] in ("starting", "running"), "state %s %s" % (st["state"], st["message"]))
    seen = {}
    if res["%s-2" % row]:
        seen = watch(vmid, logp, a.timeout, distro, say, backend, a.grub_edit)
    fw = seen.get("fw")
    rowres(3, bool(fw) if started else None, fw or "no firmware output")
    rowres(4, bool(seen.get("grub")) if started else None, seen.get("grub") or "no GRUB menu")
    rowres(5, bool(seen.get("linux")) if started else None, seen.get("linux") or "no Linux start")
    kc = "" if seen.get("kconsole") else " (no kernel log on ttyS0; GRUB edit: %s)" % seen.get("edit", "off")
    rowres(6, bool(seen.get("pool")) if started else None, (seen.get("pool") or "no restricted pool line") + kc)
    rowres(7, bool(seen.get("user")) if started else None, (seen.get("user") or "no installer userspace") + kc)

    ok = helper("stop", vmid)
    for _ in range(40):
        if backend.state(vmid)["state"] == "stopped":
            break
        time.sleep(1)
    st = backend.state(vmid)["state"]
    try:
        with open(logp, "rb") as f:
            data = f.read()
        with open(os.path.join(home, "vm-iso-test-%s-console.log" % row), "wb") as f:
            f.write(data)
    except OSError:
        pass
    note = []
    if not a.keep_vm and st == "stopped":
        storage.delete_vm(home, cfg)
        note.append("VM deleted")
    if a.delete_iso and st == "stopped":
        try:
            os.unlink(iso)
            note.append("ISO deleted")
        except OSError as e:
            note.append("ISO: %s" % e.strerror)
    rowres(8, ok and st == "stopped", "state %s; %s" % (st, ", ".join(note) or "VM kept"))
    return finish(res, say)


def finish(res, say):
    fails = [k for k, v in res.items() if v is False]
    say("RESULT %s (%d PASS, %d FAIL, %d NOT-RUN)" % ("PASS" if not fails else "FAIL " + " ".join(fails),
        sum(1 for v in res.values() if v), len(fails), sum(1 for v in res.values() if v is None)))
    return 1 if fails else 0


def watch(vmid, logp, timeout, distro, say, backend, grub_edit=False):
    """Follow console.log; edit the GRUB entry once over the console socket."""
    seen = {}
    pats = {
        "fw": [rb"UEFI firmware", rb"BdsDxe", rb"EDK II", rb"Press ESCAPE", rb"Tianocore", rb"UEFI Interactive Shell"],
        "grub": [rb"GNU GRUB", rb"Try or Install Ubuntu", rb"Install Ubuntu Server", rb"NixOS 2[0-9]\.[0-9]+ Installer",
                 rb"Use the .* keys to select"],
        "linux": [rb"EFI stub: Booting Linux", rb"Booting Linux on physical CPU", rb"Linux version [0-9]"],
        "pool": [rb"restricted DMA pool", rb"restricted-dma-pool", rb"Reserved memory: created restricted"],
        "user": [p.encode() for p in USERSPACE[distro]],
        "kconsole": [rb"\[ *[0-9]+\.[0-9]+\] "],
    }
    sock = None
    end = time.time() + timeout
    off = 0
    buf = b""
    edit_state = None
    while time.time() < end:
        try:
            with open(logp, "rb") as f:
                f.seek(off)
                d = f.read()
                off = f.tell()
        except OSError:
            d = b""
        if d:
            buf += d
            clean = ANSI.sub(b"", buf[-400000:])
            for k, ps in pats.items():
                if k in seen:
                    continue
                for p in ps:
                    m = re.search(p, clean)
                    if m:
                        s = clean[max(0, m.start() - 20):m.end() + 60].decode("utf-8", "replace")
                        seen[k] = " ".join(s.split())[:140]
                        say("  seen %s: %s" % (k, seen[k]))
                        break
        if "grub" in seen and edit_state is None and not grub_edit:
            edit_state = "off"                   # default entry boots after GRUB's timeout (or Enter below)
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s0:
                    s0.connect(os.path.join(os.path.dirname(logp), "console.sock"))
                    time.sleep(2)
                    s0.sendall(b"\r")            # boot the default entry now
                seen["edit"] = "off (Enter on the default entry)"
            except OSError as e:
                seen["edit"] = "off (Enter failed: %s)" % e
            say("  GRUB: %s" % seen["edit"])
        if "grub" in seen and edit_state is None:
            edit_state = "start"
            try:
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                sock.connect(os.path.join(os.path.dirname(logp), "console.sock"))
                mark = len(buf)
                sock.sendall(b"e")
                time.sleep(2.5)
                with open(logp, "rb") as f:
                    f.seek(off)
                    d = f.read()
                    off = f.tell()
                buf += d
                screen = ANSI.sub(b"\n", buf[mark:]).decode("utf-8", "replace")
                lines = [ln.strip() for ln in screen.split("\n") if ln.strip()]
                # the editor shows the entry body; count lines before the first 'linux' command
                body = []
                for ln in lines:
                    if re.match(r"(setparams|set |linux|initrd|search|insmod|echo|load_video|gfxmode|if |fi|else)", ln):
                        if ln not in body:
                            body.append(ln)
                idx = next((i for i, ln in enumerate(body) if re.match(r"linux", ln)), None)
                if idx is None:                      # GRUB 2.12 editor: line 0 = setparams 'title'
                    idx = 2 if distro == "ubuntu" else 1
                    how = "guessed linux line %d" % idx
                else:
                    how = "linux line %d of %d" % (idx, len(body))
                sock.sendall(b"\x0e" * idx)          # ctrl-n: next line
                time.sleep(0.3)
                sock.sendall(b"\x05")                # ctrl-e: end of line
                time.sleep(0.2)
                sock.sendall(b" console=ttyS0,115200")
                time.sleep(0.5)
                sock.sendall(b"\x18")                # ctrl-x: boot
                edit_state = "sent (%s)" % how
            except OSError as e:
                edit_state = "failed: %s" % e
            seen["edit"] = edit_state
            say("  GRUB edit %s" % edit_state)
        if all(k in seen for k in ("linux", "pool", "user")):
            break
        st = backend.state(vmid)["state"]
        if st in ("stopped", "failed"):
            say("  VM %s during the watch" % st)
            break
        time.sleep(1)
    if sock:
        sock.close()
    return seen


if __name__ == "__main__":
    sys.exit(main())
