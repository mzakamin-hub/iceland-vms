"""Host facts and guards: battery / charger, memory, CPU topology, temperature, installed pieces.

Read-only, works as the user and as root. All sysfs reads tolerate missing files (the PC
test environment has no battery and no Gunyah).
"""
import glob
import json
import os
import subprocess

from . import config

# what is proven on the tablet; the coordinator flips entries in /etc/iceland-vms/features.json
DEFAULT_FEATURES = {
    "direct_kernel": True,      # V1 / V1b PASSED 23 Oct 2026
    "network_nat": True,        # V2 PASSED 23 Oct 2026 (Fedora 44 Cloud, DHCP, DNS, HTTPS, dnf)
    "gpu2d": True,              # V3 PASSED 23 Oct 2026 (connector connected, crosvm xdg_toplevel on Mutter)
    "uefi": True,               # edk2-gunyah: untested on the tablet (shown as such in the UI)
    "gpu3d_virgl": True,        # V4 24 Oct: virgl GL on the Adreno 840 in the guest, glmark2 9253 / 9282 (0.3-8)
    "gpu3d_native": True,      # 0.2-24 (kit-vm23 N3: Adreno 840, glmark2 2691)
    "simplefb": True,           # vm-run v8 (q90r, 24 Oct): V8-0..5 PASS: guest simpledrm, host frame drawn, window
    "gpu3d_venus": True,        # 0.3-8 24 Oct: VKBLOBTEST PASS (136 MiB through host-visible coherent memory, 9 accepts) = GuestAccept proven
    "sound": False,             # crosvm has no PipeWire backend
    "qemu_backend": False,      # qemu-gunyah: on in data/features.json since 0.2-19 (QG5-QG7 + the QEMUAPP rows)
    "qemu_gl": False,           # virgl GL on QEMU (virtio-gpu-gl-pci, egl-headless / gtk gl=on): on in features.json since 0.2-19
    "memory_shared": True,      # 0.2-18: shared (pseudo-unprotected) memory mode, PV-A-1..7 PASS 26 Oct (pvm53, q90u)
    "memory_shared_3d": True,   # 0.2-20: 3D OpenGL (virgl) in the shared mode, gvm 0.3-57 PV-B-3 PASS 26 Oct (glmark2 1634)
    "memory_shared_vulkan": True,   # 0.2-22: 3D Vulkan (venus) + zink in the shared mode, kit-vm21 S rows PASS 26 Oct
    "max_running": 1,           # one VM at a time (Gunyah RM / memory)
    "min_battery_percent": 20,  # refuse a start below this when not charging
    "stop_battery_percent": 8,  # running VMs are shut down below this when not charging
    "max_start_temp_c": 85,     # refuse a start above this skin / CPU temperature
    "max_proven_memory_mib": 4096,  # largest guest RAM run on the tablet (M1 24 Oct: 4 GiB, 5 vCPU); above: a warning
}
UNTESTED = {"uefi": "UEFI firmware boot has not been run on the tablet yet"}


def _read(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return default


def features():
    f = dict(DEFAULT_FEATURES)
    for p in (os.path.join(config.SHARE_DIR, "features.json"), os.path.join(config.ETC_DIR, "features.json")):
        try:
            with open(p, encoding="utf-8") as fh:
                data = json.load(fh)
            f.update({k: v for k, v in data.items() if k in DEFAULT_FEATURES})
        except (OSError, ValueError):
            pass
    return f


def power():
    """(capacity percent or None, on_charger bool, status str)."""
    cap, on, status = None, False, "unknown"
    for s in glob.glob("/sys/class/power_supply/*"):
        t = _read(os.path.join(s, "type"), "")
        if t == "Battery":
            c = _read(os.path.join(s, "capacity"))
            if c and c.lstrip("-").isdigit():
                cap = int(c)
            status = _read(os.path.join(s, "status"), "unknown")
        elif t in ("USB", "USB_PD", "USB_C", "Mains", "Wireless"):
            if _read(os.path.join(s, "online")) == "1":
                on = True
    if status in ("Charging", "Full"):
        on = True
    return cap, on, status


def meminfo():
    out = {}
    for line in (_read("/proc/meminfo", "") or "").splitlines():
        k, _, v = line.partition(":")
        v = v.strip().split()
        if v and v[0].isdigit():
            out[k] = int(v[0]) // 1024     # MiB
    return out


def meminfo_raw():
    """/proc/meminfo values as integers (kB fields in kB, counts as counts)."""
    out = {}
    for line in (_read("/proc/meminfo", "") or "").splitlines():
        k, _, v = line.partition(":")
        v = v.strip().split()
        if v and v[0].isdigit():
            out[k] = int(v[0])
    return out


def max_guest_mib(swiotlb_mib=128):
    """Largest guest RAM that keeps HOST_RESERVE_MIB for the host (by MemTotal, rounded to 256)."""
    total = meminfo().get("MemTotal", 0)
    m = total - config.HOST_RESERVE_MIB - swiotlb_mib - 512     # 512: page tables, crosvm itself
    m = max(256, min(config.LIMITS["memory_mib"][1], m))
    return m // 256 * 256


def cpu_topology():
    """{cpu: capacity}; big = the highest-capacity cluster."""
    caps = {}
    for p in glob.glob("/sys/devices/system/cpu/cpu[0-9]*"):
        n = os.path.basename(p)[3:]
        if not n.isdigit():
            continue
        c = _read(os.path.join(p, "cpu_capacity"))
        caps[int(n)] = int(c) if c and c.isdigit() else 1024
    return dict(sorted(caps.items()))


def cpu_sets():
    caps = cpu_topology()
    if not caps:
        n = os.cpu_count() or 1
        caps = {i: 1024 for i in range(n)}
    top = max(caps.values())
    big = [c for c, v in caps.items() if v == top]
    little = [c for c, v in caps.items() if v != top]
    return {"all": list(caps), "big": big, "little": little or list(caps)}


def max_vcpus():
    """Host-safe vCPU count: leave at least two host CPUs free."""
    return max(1, min(config.LIMITS["cpus"][1], (os.cpu_count() or 2) - 2))


def max_temp_c():
    best = None
    for z in glob.glob("/sys/class/thermal/thermal_zone*"):
        t = _read(os.path.join(z, "type"), "")
        if not any(k in t for k in ("cpu", "skin", "xo", "soc", "aoss")):
            continue
        v = _read(os.path.join(z, "temp"))
        if v and v.lstrip("-").isdigit():
            c = int(v) / 1000.0
            if -40 < c < 150:
                best = c if best is None else max(best, c)
    return best


def kernel():
    return os.uname().release


def gvm_installed():
    return os.access(os.path.join(config.GVM_DIR, "crosvm"), os.X_OK)


def gvm_version():
    return cached("gvm_version", _gvm_version)


def _gvm_version():
    try:
        r = subprocess.run(["rpm", "-q", "--qf", "%{VERSION}-%{RELEASE}", "iceland-gvm"],
                           capture_output=True, text=True, timeout=10)
        return r.stdout.strip() if r.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


_facts = {}


def forget():
    """Drop the cached host facts (the window's Refresh; a package may have changed)."""
    _facts.clear()


def cached(name, fn):
    """fn() once per process (or until forget()): rpm / modinfo cost a fork each and do not change while the
    kernel runs (0.2-26: the window ran them on its main thread at every list reload)."""
    if name not in _facts:
        _facts[name] = fn()
    return _facts[name]


def modules_match():
    """True when Gunyah host modules for the running kernel exist (in the kernel, or in an old iceland-gvm payload)."""
    return cached("modules_match", _modules_match)


def _modules_match():
    try:
        if subprocess.run(["modinfo", "-n", "gunyah_rsc_mgr"], capture_output=True, timeout=10).returncode == 0:
            return True
    except (OSError, subprocess.TimeoutExpired):
        pass
    ko = os.path.join(config.GVM_DIR, "mods", "gunyah.ko")
    try:
        r = subprocess.run(["modinfo", "-F", "vermagic", ko], capture_output=True, text=True, timeout=10)
        return bool(r.stdout) and r.stdout.split()[0] == kernel()
    except (OSError, subprocess.TimeoutExpired, IndexError):
        return False


def gunyah_loaded():
    return os.path.exists("/dev/gunyah")


def guest_modules_tarball():
    g = sorted(glob.glob(os.path.join(config.GVM_DIR, "guest", "modules-*.tar.gz")))
    return g[-1] if g else None


def guest_kernel_release():
    t = guest_modules_tarball()
    if not t:
        return None
    return os.path.basename(t)[len("modules-"):-len(".tar.gz")]


MIN_3D_KERNEL_BUILD = 529      # q90r (7.3.0-next-20261020-iceland-00529-...): first host kernel with a drawn 3D guest


def kernel_build():
    """The NNNNN of an iceland kernel release (...-iceland-NNNNN-g<sha>), else None."""
    import re
    m = re.search(r"-iceland-(\d+)-g", kernel())
    return int(m.group(1)) if m else None


def start_warnings(cfg):
    """Non-blocking warnings shown before a start."""
    w = []
    b = kernel_build()
    if cfg["display"]["gpu"] in ("virgl", "venus", "native", "zink") and b is not None and b < MIN_3D_KERNEL_BUILD:
        w.append("This host kernel (%s) is older than q90r (build 00529): the 3D window may stay black. "
                 "Update the kernel, or use the 2D display" % kernel())
    return w


def start_guard(cfg, feats=None, running=0):
    """List of human-readable reasons why cfg must not start now (empty = OK)."""
    f = feats or features()
    why = []
    if running >= f["max_running"]:
        why.append("Another virtual machine is running (at most %d at a time)" % f["max_running"])
    cap, on, _ = power()
    if cap is not None and not on and cap < f["min_battery_percent"]:
        why.append("Battery %d %% and no charger: connect the charger (needs %d %%)" % (cap, f["min_battery_percent"]))
    m = meminfo()
    sw = 0 if cfg.get("memory_mode") == "shared" else cfg["swiotlb_mib"]     # shared mode: no bounce pool
    need = cfg["memory_mib"] + sw + config.HOST_RESERVE_MIB
    if m.get("MemAvailable", 0) and m["MemAvailable"] < need:
        why.append("Not enough free memory: %d MiB available, %d MiB needed (guest %d + swiotlb %d + %d for the host)"
                   % (m["MemAvailable"], need, cfg["memory_mib"], sw, config.HOST_RESERVE_MIB))
    t = max_temp_c()
    if t is not None and t > f["max_start_temp_c"]:
        why.append("The tablet is hot (%.0f C): let it cool down" % t)
    return why
