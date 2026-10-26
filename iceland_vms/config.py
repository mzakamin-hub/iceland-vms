"""VM configuration model, shared by the GUI (runs as the user) and the root helper.

Every value that reaches the root helper is checked here with strict patterns and ranges.
File references are never free paths: a disk is (store, name) where store is one of the
user's own storage folders and name is a plain file name; the helper resolves them itself.
"""
import copy
import json
import os
import re
import secrets

SCHEMA = 1

# ---- installed locations (the rpm layout) -------------------------------------------------
GVM_DIR = os.environ.get("IVMS_GVM_DIR", "/usr/libexec/iceland-gvm")      # iceland-gvm >= 0.2
SHARE_DIR = os.environ.get("IVMS_SHARE_DIR", "/usr/share/iceland-vms")   # firmware, guest tools, catalogue
HELPER = "/usr/libexec/iceland-vms/iceland-vms-helper"
ETC_DIR = "/etc/iceland-vms"
RUN_DIR = "/run/iceland-vms"
FIRMWARE = os.path.join(SHARE_DIR, "firmware", "edk2-gunyah.fd")
FIRMWARE_DEBUG = os.path.join(SHARE_DIR, "firmware", "edk2-gunyah-debug.fd")
# 0.2-12 (Windows agent): the ACPI + PCI ECAM build of edk2-gunyah that boots Windows 11 ARM64 (gvm.sh win, vm-run
# WIN v10l: desktop, input, network, RDP, 30 min soak). It ships with iceland-gvm; crosvm needs CROSVM_GVM_PCI_ECAM=1.
FIRMWARE_ECAM = os.path.join(GVM_DIR, "firmware", "edk2-gunyah-ecam-release.fd")
FIRMWARE_ECAM_DEBUG = os.path.join(GVM_DIR, "firmware", "edk2-gunyah-ecam-debug.fd")
FIRMWARES = ("standard", "ecam")        # boot.firmware for UEFI VMs
WINDOWS_OS = "windows-11-arm64"         # cfg["os"] of an imported Windows image
# disk I/O engine of crosvm's virtio-blk (DiskOption async-executor): io_uring won vm-run OPT v10m (2 MiB lends,
# read 692 -> 3300 MB/s, write 701 -> 1300 MB/s); epoll stays selectable
DISK_ASYNC = ("uring", "epoll")
QEMU_BIN = "/usr/libexec/iceland-qemu-gunyah/qemu-system-aarch64"

ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}$")
FILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")
MAC_RE = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")
TAG_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
ROOT_RE = re.compile(r"^(PARTUUID=[0-9a-fA-F-]{8,36}|/dev/vd[a-z][0-9]{0,2}|LABEL=[A-Za-z0-9._-]{1,32})$")
FSTYPE_RE = re.compile(r"^(ext4|xfs|btrfs|f2fs|ext3|ext2)$")
ROOTFLAGS_RE = re.compile(r"^[A-Za-z0-9@._/=,-]{0,96}$")
CMDLINE_RE = re.compile(r"^[A-Za-z0-9 ._=,:/@+\-]{0,512}$")
CPUSET_RE = re.compile(r"^(\d{1,2}(-\d{1,2})?)(,\d{1,2}(-\d{1,2})?)*$")

STORES = ("vm", "isos", "images")       # vm = the VM's own folder, isos / images = shared download folders
DISK_KINDS = ("disk", "cdrom")
BOOT_MODES = ("direct", "uefi")
# 0.2-10 (step 4): "protected" = guest RAM lent to Gunyah, the host cannot read it (default);
# "shared" = DroidVM-style pseudo-unprotected mode: RAM shared at run time and accepted by a boot shim,
# THE HOST CAN READ AND WRITE ALL OF THE VM'S MEMORY. Opt-in per VM, crosvm only.
MEMORY_MODES = ("protected", "shared")
NET_MODES = ("off", "nat")
# 0.2-10: RDP clients the app can start, in order of preference (KDE first), and their command lines
RDP_CLIENTS = ("krdc", "remmina", "sdl-freerdp", "sdl-freerdp3", "xfreerdp", "xfreerdp3", "wlfreerdp")


def rdp_client_argv(prog, ip):
    if prog == "krdc":
        return [prog, "rdp://%s" % ip]
    if prog == "remmina":
        return [prog, "-c", "rdp://%s" % ip]
    return [prog, "/v:%s" % ip, "/dynamic-resolution", "/cert:tofu"]


def is_windows(cfg):
    """A Windows guest: the catalogue / import id starts with "windows" (e.g. windows-11-arm64)."""
    return str(cfg.get("os", "")).lower().startswith("windows")


INPUT_MODES = ("tablet", "touchpad", "touch")   # 0.2-6: crosvm CROSVM_GVM_INPUT_MODE
GPU_MODES = ("none", "2d", "virgl", "native", "venus", "simplefb", "zink")   # native = msm DRM native context;
# simplefb = DroidVM's default screen: a framebuffer in host-shared memory, guest simpledrm, crosvm --simplefb (0.2-1)
SOUND_MODES = ("off", "null", "file")
SHARE_KINDS = ("p9", "fs")
AFFINITY = ("any", "big", "little", "custom")
BACKENDS = ("auto", "crosvm", "qemu")     # auto: qemu only when USB passthrough is on, else crosvm

LIMITS = {
    "cpus": (1, 8),
    "memory_mib": (256, 8192),
    # 320: the guest kernel's restricted DMA pool keeps one 24-byte slot per 2 KiB in ONE kmalloc (4 MiB at most with
    # 4 KiB pages, MAX_PAGE_ORDER 10): 512 MiB needs 6 MiB, the pool fails and the guest has no disk (vm-run OPT S5)
    "swiotlb_mib": (64, 320),
    "disk_gib": (1, 512),
    "width": (640, 3840),
    "height": (480, 3000),
    "dpi": (0, 600),
    "port": (1, 65535),
    "vnc_port": (5900, 5999),
    "disks": 6,
    "shares": 4,
    "forwards": 16,
}
HOST_RESERVE_MIB = 3072          # the owner's rule: keep >= 3 GiB for the host


class ConfigError(ValueError):
    pass


# ---- per-user storage ---------------------------------------------------------------------
def data_home(home):
    return os.path.join(home, ".local", "share", "gvm")


def store_dir(home, store, vmid=None):
    base = data_home(home)
    if store == "vm":
        if vmid is None:
            raise ConfigError("store vm needs a VM id")
        return os.path.join(base, "vms", vmid)
    if store in ("isos", "images"):
        return os.path.join(base, store)
    raise ConfigError("unknown store %r" % store)


def vm_dir(home, vmid):
    return store_dir(home, "vm", vmid)


def run_dir(uid, vmid=None):
    d = os.path.join(RUN_DIR, "u%d" % uid)
    return os.path.join(d, vmid) if vmid else d


def unit_name(uid, vmid):
    return "iceland-vm-u%d-%s.service" % (uid, vmid)


def random_mac():
    # locally administered, unicast; 52:54:00 is QEMU's, use our own 0a:1c prefix
    b = [0x0a, 0x1c] + [secrets.randbelow(256) for _ in range(4)]
    return ":".join("%02x" % x for x in b)


# ---- defaults -----------------------------------------------------------------------------
def default_config(vmid, name):
    return {
        "schema": SCHEMA,
        "id": vmid,
        "name": name,
        "os": "custom",                 # catalogue entry id or "custom"
        "backend": "auto",
        "cpus": 4,
        "cpu_affinity": "any",          # any | big | little | custom
        "cpu_set": "",                  # host CPUs for "custom", e.g. "0-5"
        "memory_mib": 2048,
        "swiotlb_mib": 128,
        # 0.2-10: 2 MiB lends by default (crosvm --hugepages + shmem THP): with 4 KiB lends every guest page costs a
        # TrustZone call and a Windows guest froze the host (vm-run WINN vs WINH, 25 Oct)
        "hugepages": True,
        "mem_defaults_rev": 1,
        "disk_async": "uring",          # 0.2-12: io_uring for every disk (DISK_ASYNC)
        "memory_mode": "protected",     # protected | shared (host can read the VM's memory; see MEMORY_MODES)
        "boot": {
            "mode": "direct",           # direct = our guest kernel; uefi = edk2-gunyah firmware
            "root": "",                 # direct: PARTUUID=... or /dev/vdaN
            "rootfstype": "",
            "rootflags": "",
            "cmdline_extra": "",
            "initrd": False,            # direct: our test initramfs (busybox) instead of a disk root
            "firmware_debug": False,    # uefi: the DEBUG edk2-gunyah build (verbose serial log)
            "firmware": "standard",     # uefi: standard = edk2-gunyah (Linux ISOs); ecam = ACPI + PCI ECAM (Windows)
        },
        "disks": [],                    # [{"store": "vm", "name": "disk0.raw", "kind": "disk", "ro": False}]
        # vhost-net (in-kernel virtio-net datapath, host CONFIG_VHOST_NET=m since q90s): ON by default since 0.2-15
        # (fix run V11 on q90u: guest -> host 2434 -> 3087 MB/s, host -> guest 1970 -> 4707 MB/s); a kernel without
        # it falls back to crosvm's own network (helper); switchable in Settings > Network
        "network": {"mode": "nat", "mac": random_mac(), "forwards": [], "vhost_net": True},
        # 0.2-4: 3D (virgl GL + venus Vulkan) with a GPU-composited desktop is the default (vm-run v10d: Xorg
        # glamor and weston GL drawn with the iceland-gvm 0.3-23 guest kernel); the software display (simplefb,
        # vm-run v8) stays the fallback
        "display": {"gpu": "venus", "width": 1600, "height": 1000, "dpi": 0,
                    "keyboard": True, "mouse": True, "touch": False,
                    # Xorg glamor in the guest: ON again since 0.2-4 (vm-run v10d). It was off from 0.1-10 because
                    # texture uploads through virgl staging reached the host as zeros (black GL desktops); the
                    # guest kernel of iceland-gvm 0.3-23 syncs those buffers through the swiotlb.
                    "glamor": True,
                    "autologin": True,   # desktop autologin: touch / the host OSK cannot type into a greeter
                    # 3D guests: Xorg (the session proven to draw, without glamor, vm-run v5); "wayland" autologins
                    # into labwc when the guest has it (not yet drawn: GL compositors stay black)
                    "session": "x11",
                    # 0.2-6 (DroidVM parity): how the window's touch reaches the guest - "tablet" (tap = click,
                    # long press = right click, two fingers scroll, drag = drag), "touchpad" (relative pointer like
                    # a laptop touchpad) or "touch" (the guest's own multi-touch gestures)
                    "input_mode": "tablet",
                    "fullscreen": False,     # open the VM window fullscreen ...
                    "output": "",            # ... on this monitor (connector name, e.g. "DP-1"; "" = any)
                    # ... with the guest display at that monitor's resolution (set by the app before a start;
                    # otherwise crosvm scales the VM's width x height to the screen)
                    "fullscreen_native": True,
                    # 0.2-7 (DroidVM parity): serve the display over VNC instead of the window (crosvm
                    # --vnc-server / QEMU -vnc); vnc_lan listens on every address (needs a password, kept in
                    # the VM folder as vnc-password, never in vm.json)
                    "vnc": False,
                    "vnc_lan": False,
                    "vnc_port": 5900,
                    "vnc_password": False,
                    "defaults_rev": 2},
        "sound": {"mode": "off"},
        "shares": [],                   # [{"path": "~/Public", "tag": "share0", "kind": "p9", "ro": True}]
        "cloud_init": {"enabled": False, "user": "gvm", "seed": "seed.img"},
        "guest_tools": True,            # 9p share with our guest kernel modules (+ gunyah_guest.ko)
        # 0.2-12: Windows guests: the account the app's clean shutdown logs in with over ssh (key only: the guest's
        # OpenSSH accepts the app's key or the user's own ~/.ssh key; NO password is ever stored)
        "windows": {"ssh_user": "gvm"},
        "usb": [],                      # qemu backend only: [{"vendor": "0781", "product": "5581"}]
        "autostart": False,
        "notes": "",
    }


def merged(cfg):
    """Fill missing keys from the defaults (forward compatibility of older vm.json files)."""
    base = default_config(cfg.get("id", "x"), cfg.get("name", "VM"))
    out = copy.deepcopy(base)
    for k, v in cfg.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k].update(v)
        else:
            out[k] = v
    return out


def fill_defaults(cfg):
    """In place: add keys missing from an older vm.json (e.g. display.glamor written by 0.1-3),
    so validate() / save() accept a file a script loaded with plain json.load()."""
    if not isinstance(cfg, dict):
        return cfg
    base = default_config(cfg.get("id", "x"), cfg.get("name", "VM"))
    for k, v in base.items():
        if k == "schema":
            continue                    # never invent the schema of an unknown file
        if k not in cfg:
            cfg[k] = copy.deepcopy(v)
        elif isinstance(v, dict) and isinstance(cfg[k], dict):
            for kk, vv in v.items():
                cfg[k].setdefault(kk, copy.deepcopy(vv))
    return cfg


# ---- validation ---------------------------------------------------------------------------
def _int(cfg, key, lo, hi, where=""):
    v = cfg.get(key)
    if not isinstance(v, int) or isinstance(v, bool) or not lo <= v <= hi:
        raise ConfigError("%s%s must be an integer %d..%d (got %r)" % (where, key, lo, hi, v))
    return v


def _bool(d, key, where=""):
    if not isinstance(d.get(key), bool):
        raise ConfigError("%s%s must be true/false" % (where, key))


def _str(d, key, rx, where="", allow_empty=True):
    v = d.get(key, "")
    if not isinstance(v, str) or (not v and not allow_empty) or (v and not rx.match(v)):
        raise ConfigError("%s%s has an invalid value %r" % (where, key, v))
    return v


def validate(cfg, features=None):
    """Raise ConfigError unless cfg is a complete, safe VM description. Returns cfg.
    Keys missing from an older vm.json are filled from the defaults first (in place)."""
    fill_defaults(cfg)
    if not isinstance(cfg, dict) or cfg.get("schema") != SCHEMA:
        raise ConfigError("not a schema-%d VM description" % SCHEMA)
    if not isinstance(cfg.get("id"), str) or not ID_RE.match(cfg["id"]):
        raise ConfigError("id must match %s" % ID_RE.pattern)
    name = cfg.get("name")
    if not isinstance(name, str) or not 0 < len(name) <= 64 or any(ord(c) < 32 for c in name):
        raise ConfigError("name must be 1-64 printable characters")
    if cfg.get("backend") not in BACKENDS:
        raise ConfigError("backend must be one of %s" % (BACKENDS,))
    _int(cfg, "cpus", *LIMITS["cpus"])
    if cfg.get("cpu_affinity") not in AFFINITY:
        raise ConfigError("cpu_affinity must be one of %s" % (AFFINITY,))
    if cfg["cpu_affinity"] == "custom":
        _str(cfg, "cpu_set", CPUSET_RE, allow_empty=False)
    _int(cfg, "memory_mib", *LIMITS["memory_mib"])
    _int(cfg, "swiotlb_mib", *LIMITS["swiotlb_mib"])
    _bool(cfg, "hugepages")
    if cfg.get("disk_async") not in DISK_ASYNC:
        raise ConfigError("disk_async must be one of %s" % (DISK_ASYNC,))
    if cfg.get("memory_mode") not in MEMORY_MODES:
        raise ConfigError("memory_mode must be one of %s" % (MEMORY_MODES,))

    b = cfg.get("boot")
    if not isinstance(b, dict) or b.get("mode") not in BOOT_MODES:
        raise ConfigError("boot.mode must be one of %s" % (BOOT_MODES,))
    _str(b, "root", ROOT_RE, "boot.")
    _str(b, "rootfstype", FSTYPE_RE, "boot.")
    _str(b, "rootflags", ROOTFLAGS_RE, "boot.")
    _str(b, "cmdline_extra", CMDLINE_RE, "boot.")
    _bool(b, "initrd", "boot.")
    _bool(b, "firmware_debug", "boot.")
    if b.get("firmware") not in FIRMWARES:
        raise ConfigError("boot.firmware must be one of %s" % (FIRMWARES,))
    if b["mode"] == "direct" and not b["initrd"] and not b["root"]:
        raise ConfigError("direct kernel boot needs a root device (use Detect in Boot settings)")

    disks = cfg.get("disks")
    if not isinstance(disks, list) or len(disks) > LIMITS["disks"]:
        raise ConfigError("disks must be a list of at most %d entries" % LIMITS["disks"])
    for i, d in enumerate(disks):
        w = "disks[%d]." % i
        if not isinstance(d, dict) or d.get("store") not in STORES:
            raise ConfigError(w + "store must be one of %s" % (STORES,))
        _str(d, "name", FILE_RE, w, allow_empty=False)
        if d.get("kind") not in DISK_KINDS:
            raise ConfigError(w + "kind must be one of %s" % (DISK_KINDS,))
        _bool(d, "ro", w)
        if d["kind"] == "cdrom" and not d["ro"]:
            raise ConfigError(w + "a CD/ISO image is always read-only")
        if d["store"] != "vm" and not d["ro"]:
            raise ConfigError(w + "shared ISO / image files can only be attached read-only")

    n = cfg.get("network")
    if not isinstance(n, dict) or n.get("mode") not in NET_MODES:
        raise ConfigError("network.mode must be one of %s" % (NET_MODES,))
    _str(n, "mac", MAC_RE, "network.", allow_empty=False)
    _bool(n, "vhost_net", "network.")
    fw = n.get("forwards", [])
    if not isinstance(fw, list) or len(fw) > LIMITS["forwards"]:
        raise ConfigError("network.forwards: at most %d" % LIMITS["forwards"])
    seen = set()
    for i, f in enumerate(fw):
        w = "network.forwards[%d]." % i
        if not isinstance(f, dict) or f.get("proto") not in ("tcp", "udp"):
            raise ConfigError(w + "proto must be tcp or udp")
        _int(f, "host", 1024, 65535, w)       # no privileged host ports
        _int(f, "guest", *LIMITS["port"], where=w)
        if (f["proto"], f["host"]) in seen:
            raise ConfigError(w + "duplicate host port")
        seen.add((f["proto"], f["host"]))

    dsp = cfg.get("display")
    if not isinstance(dsp, dict) or dsp.get("gpu") not in GPU_MODES:
        raise ConfigError("display.gpu must be one of %s" % (GPU_MODES,))
    _int(dsp, "width", *LIMITS["width"], where="display.")
    _int(dsp, "height", *LIMITS["height"], where="display.")
    _int(dsp, "dpi", *LIMITS["dpi"], where="display.")
    for k in ("keyboard", "mouse", "touch", "glamor", "autologin"):
        _bool(dsp, k, "display.")
    if dsp.get("session") not in ("wayland", "x11"):
        raise ConfigError("display.session must be wayland or x11")
    if dsp.get("input_mode") not in INPUT_MODES:
        raise ConfigError("display.input_mode must be one of %s" % (INPUT_MODES,))
    _bool(dsp, "fullscreen", "display.")
    _bool(dsp, "fullscreen_native", "display.")
    for k in ("vnc", "vnc_lan", "vnc_password"):
        _bool(dsp, k, "display.")
    _int(dsp, "vnc_port", *LIMITS["vnc_port"], where="display.")
    if dsp["vnc"] and dsp["gpu"] == "none":
        raise ConfigError("the VNC display needs a display (not \"none\")")
    if dsp["vnc"] and dsp["vnc_lan"] and not dsp["vnc_password"]:
        raise ConfigError("a VNC display reachable from the network needs a password")
    out = dsp.get("output")
    if not isinstance(out, str) or len(out) > 63 or not all(ch.isalnum() or ch in "._:-" for ch in out):
        raise ConfigError("display.output must be a monitor connector name like DP-1 (or empty)")

    s = cfg.get("sound")
    if not isinstance(s, dict) or s.get("mode") not in SOUND_MODES:
        raise ConfigError("sound.mode must be one of %s" % (SOUND_MODES,))

    shares = cfg.get("shares")
    if not isinstance(shares, list) or len(shares) > LIMITS["shares"]:
        raise ConfigError("shares: at most %d" % LIMITS["shares"])
    tags = set()
    for i, sh in enumerate(shares):
        w = "shares[%d]." % i
        if not isinstance(sh, dict) or sh.get("kind") not in SHARE_KINDS:
            raise ConfigError(w + "kind must be p9 or fs")
        p = sh.get("path")
        if not isinstance(p, str) or not p.startswith("/") or "\0" in p or len(p) > 512 \
                or os.path.normpath(p) != p:
            raise ConfigError(w + "path must be a normalised absolute path")
        _str(sh, "tag", TAG_RE, w, allow_empty=False)
        if sh["tag"] in tags or sh["tag"] == "gvmshare":
            raise ConfigError(w + "duplicate or reserved tag")
        tags.add(sh["tag"])
        _bool(sh, "ro", w)

    ci = cfg.get("cloud_init")
    if not isinstance(ci, dict):
        raise ConfigError("cloud_init must be an object")
    _bool(ci, "enabled", "cloud_init.")
    _str(ci, "user", re.compile(r"^[a-z_][a-z0-9_-]{0,31}$"), "cloud_init.", allow_empty=False)
    _str(ci, "seed", FILE_RE, "cloud_init.", allow_empty=False)
    _bool(cfg, "guest_tools")
    _bool(cfg, "autostart")
    w = cfg.get("windows")
    if not isinstance(w, dict):
        raise ConfigError("windows must be an object")
    _str(w, "ssh_user", re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,31}$"), "windows.")
    if is_windows(cfg) and (b["mode"] != "uefi" or b["firmware"] != "ecam"):
        raise ConfigError("a Windows VM boots through the ECAM UEFI firmware (boot.mode uefi, boot.firmware ecam)")

    usb = cfg.get("usb", [])
    if not isinstance(usb, list) or len(usb) > 8:
        raise ConfigError("usb: at most 8 devices")
    for i, u in enumerate(usb):
        if not isinstance(u, dict) or not re.match(r"^[0-9a-f]{4}$", str(u.get("vendor", ""))) \
                or not re.match(r"^[0-9a-f]{4}$", str(u.get("product", ""))):
            raise ConfigError("usb[%d] needs 4-digit hex vendor and product ids" % i)
    if cfg["display"].get("vnc") and cfg["display"].get("vnc_lan") and cfg["backend"] == "qemu":
        raise ConfigError("the QEMU backend serves VNC on this tablet only (127.0.0.1); use crosvm for a network VNC display")
    if usb and cfg["backend"] == "crosvm":
        raise ConfigError("USB passthrough needs the qemu-gunyah backend (crosvm cannot pass USB to protected VMs)")
    if not isinstance(cfg.get("notes", ""), str) or len(cfg.get("notes", "")) > 2000:
        raise ConfigError("notes: at most 2000 characters")

    # feature gates (what has been proven on the tablet; /etc/iceland-vms/features.json)
    if features is not None:
        need = {"virgl": "gpu3d_virgl", "native": "gpu3d_native", "venus": "gpu3d_venus", "zink": "gpu3d_venus",
                "simplefb": "simplefb"}.get(dsp["gpu"])
        if need and not features.get(need):
            raise ConfigError("display %s is not enabled yet (feature %s not proven on this tablet)" % (dsp["gpu"], need))
        if b["mode"] == "uefi" and not features.get("uefi", True):
            raise ConfigError("UEFI boot is disabled on this tablet (feature uefi)")
        if cfg["memory_mode"] == "shared" and not features.get("memory_shared"):
            raise ConfigError("the shared memory mode is not enabled yet (feature memory_shared not proven on this tablet)")
        rb = resolve_backend(cfg, features)
        if cfg.get("usb") and rb != "qemu":
            raise ConfigError("USB passthrough needs the QEMU-Gunyah backend, which is not enabled on this tablet")
        bad = backend_conflicts(cfg, rb)
        if bad:
            raise ConfigError("the %s backend cannot provide %s: switch the backend in Settings > General"
                              % (BACKEND_LABEL[rb], ", ".join(bad)))
        if rb == "qemu" and not features.get("qemu_backend"):
            raise ConfigError("the qemu-gunyah backend is not enabled (feature qemu_backend)")
    return cfg


# ---- load / save (as the user) ------------------------------------------------------------
MEM_DEFAULTS_REV = 1          # 1 (0.2-10): hugepages on
DISPLAY_DEFAULTS_REV = 2      # 1 (0.1-10): glamor off + Xorg; 2 (0.2-4): glamor back on for 3D VMs (vm-run v10d)


def load(path):
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    rev = (raw.get("display") or {}).get("defaults_rev", 0) if isinstance(raw, dict) else 0
    cfg = merged(raw)
    d = cfg.get("display", {})
    if isinstance(d, dict) and rev < DISPLAY_DEFAULTS_REV:
        # once per VM (the user can switch back afterwards): a 3D VM gets the GPU-composited Xorg desktop
        # (glamor on, fixed by the 0.3-23 guest kernel); other displays keep glamor off. The display mode itself
        # (2D / 3D / software) is the user's choice and is not changed.
        d["glamor"] = d.get("gpu") in ("virgl", "venus", "native", "zink")
        d["session"], d["defaults_rev"] = "x11", DISPLAY_DEFAULTS_REV
    # 0.2-10: once per VM, 2 MiB guest memory pages on (the hugepages default); the user can turn it off afterwards
    if isinstance(raw, dict) and raw.get("mem_defaults_rev", 0) < MEM_DEFAULTS_REV:
        cfg["hugepages"], cfg["mem_defaults_rev"] = True, MEM_DEFAULTS_REV
    return cfg


def save(cfg, path):
    fill_defaults(cfg)
    validate(cfg)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)


def fullscreen_size(cfg, monitors):
    """0.2-6: the guest display size for a fullscreen VM with fullscreen_native, or None.

    monitors: [(connector, width_px, height_px)] of the host (the UI asks GDK). The configured output is used,
    else the first monitor; the size is clamped to LIMITS (crosvm's window buffer = the guest's resolution, so
    the fullscreen window shows it 1:1 instead of scaled)."""
    d = cfg["display"]
    if not d.get("fullscreen") or not d.get("fullscreen_native", True) or not monitors:
        return None
    m = next((x for x in monitors if x[0] == d.get("output")), None) if d.get("output") else None
    m = m or monitors[0]
    w = max(LIMITS["width"][0], min(LIMITS["width"][1], int(m[1])))
    h = max(LIMITS["height"][0], min(LIMITS["height"][1], int(m[2])))
    return w, h


VNC_PASSWORD_FILE = "vnc-password"     # in the VM's folder, mode 0600


def vnc_address(cfg):
    """(host, port) a viewer on this tablet connects to, or None without VNC."""
    d = cfg["display"]
    if not d.get("vnc"):
        return None
    return "127.0.0.1", d.get("vnc_port", 5900)


def vnc_viewer_argv(cfg, which):
    """argv for a VNC viewer program ("krdc", "vncviewer", "remmina", "gvncviewer"), or None."""
    a = vnc_address(cfg)
    if not a:
        return None
    host, port = a
    return {"krdc": ["krdc", "vnc://%s:%d" % (host, port)],
            "vncviewer": ["vncviewer", "%s::%d" % (host, port)],
            "remmina": ["remmina", "-c", "vnc://%s:%d" % (host, port)],
            "gvncviewer": ["gvncviewer", "%s:%d" % (host, port - 5900)]}.get(which)


def firmware_path(cfg):
    """The UEFI firmware file of a VM (standard edk2-gunyah from this package, or the ECAM build from iceland-gvm)."""
    b = cfg["boot"]
    if b.get("firmware") == "ecam":
        return FIRMWARE_ECAM_DEBUG if b["firmware_debug"] else FIRMWARE_ECAM
    return FIRMWARE_DEBUG if b["firmware_debug"] else FIRMWARE


# The MAC address gvm.sh win (the Windows setup / WIN rows) gives the VM: an image set up there knows its adapter by
# it, so the app VM keeps it (a new MAC is a new adapter to Windows: new network profile, firewall scope, lease).
WINDOWS_IMAGE_MAC = "0a:1c:47:56:4d:02"


def windows_defaults(cfg):
    """In place: the Windows 11 ARM64 settings proven by gvm.sh win (vm-run WIN, v10l): ECAM UEFI firmware, the
    shared-framebuffer display (simplefb), 4 vCPU / 4 GiB, 2 MiB lends, swiotlb 256 MiB, NAT, io_uring disks; no
    Linux guest tools, cloud-init or virtio-console agent (Windows has no driver for them)."""
    cfg["os"] = WINDOWS_OS
    cfg["boot"].update(mode="uefi", firmware="ecam", initrd=False, root="", rootfstype="", rootflags="")
    # input "touch": the device set gvm.sh win proved (W4-1 / W4-2: no CROSVM_GVM_INPUT_MODE = touch screen + keyboard);
    # tablet / touchpad add a virtio tablet + mouse that the Windows VirtioInput driver has not been tested with
    cfg["display"].update(gpu="simplefb", width=1600, height=1000, glamor=False, autologin=False, input_mode="touch")
    cfg.update(cpus=4, memory_mib=4096, swiotlb_mib=256, hugepages=True, disk_async="uring", guest_tools=False)
    cfg["cloud_init"]["enabled"] = False
    cfg["network"]["mode"] = "nat"
    cfg["network"]["mac"] = WINDOWS_IMAGE_MAC       # 0.2-14 (release run WA-5): the adapter the image was set up with
    cfg["sound"]["mode"] = "off"
    return cfg


def kernel_cmdline(cfg):
    """Guest kernel command line for direct boot."""
    b = cfg["boot"]
    # with a display: boot log + getty on tty1 in the window; the last console= (ttyS0) stays /dev/console
    parts = (["console=tty0"] if cfg["display"]["gpu"] != "none" else []) + ["console=ttyS0", "earlycon"]
    if b["initrd"]:
        parts += ["gvm.test=shell", "panic=-1"]
    else:
        parts += ["root=" + b["root"], "rw", "rootwait"]
        if b["rootfstype"]:
            parts.append("rootfstype=" + b["rootfstype"])
        if b["rootflags"]:
            parts.append("rootflags=" + b["rootflags"])
        parts.append("systemd.firstboot=off")
    if b["cmdline_extra"]:
        parts += b["cmdline_extra"].split()
    return " ".join(parts)


# ---- backends -------------------------------------------------------------------------------
# What each backend can do for a protected Gunyah VM. crosvm: proven (V1-V3). qemu: from the
# qemu-gunyah evaluation (qemu/backend.json); everything False until that backend is enabled.
BACKEND_CAPS = {
    "crosvm": {"usb": False, "gpu2d": True, "gpu3d": True, "simplefb": True, "wayland_window": True, "shares": True,
               "agent": True, "uefi": True, "direct": True, "snapshots_offline": True, "shared_memory": True},
    "qemu": {"usb": True, "gpu2d": True, "gpu3d": False, "simplefb": False, "wayland_window": True, "shares": True,
             "agent": True, "uefi": True, "direct": True, "snapshots_offline": True, "shared_memory": False},
}
BACKEND_LABEL = {"crosvm": "crosvm", "qemu": "QEMU-Gunyah"}


MEMORY_SHARED_WARNING = ("The tablet (host) can read and change all of this VM's memory while it runs. "
                         "Use it only for VMs you would also run unprotected on a PC. It removes the I/O bounce "
                         "buffer and lets devices reach guest memory directly.")


def resolve_backend(cfg, features=None):
    """The backend a VM really uses: Automatic picks QEMU only for USB passthrough (and only when it is enabled)."""
    b = cfg.get("backend", "auto")
    if b != "auto":
        return b
    if cfg.get("usb") and (features is None or features.get("qemu_backend")):
        return "qemu"
    return "crosvm"


def backend_conflicts(cfg, backend):
    """Human-readable list of the VM's enabled features that backend cannot provide."""
    caps = BACKEND_CAPS[backend]
    out = []
    if cfg.get("usb") and not caps["usb"]:
        out.append("USB passthrough")
    if cfg["display"]["gpu"] in ("virgl", "native", "venus", "zink") and not caps["gpu3d"]:
        out.append("3D graphics")
    if cfg["display"]["gpu"] == "simplefb" and not caps["simplefb"]:
        out.append("the shared-framebuffer software display")
    if cfg["display"]["gpu"] != "none" and not caps["wayland_window"]:
        out.append("the display window on this desktop")
    if cfg["shares"] and not caps["shares"]:
        out.append("shared folders")
    if cfg.get("memory_mode") == "shared" and not caps.get("shared_memory"):
        out.append("the shared (host-readable) memory mode")
    return out
