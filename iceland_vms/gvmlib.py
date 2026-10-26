"""Root-side operations ported from iceland-gvm's gvm.sh (load / unload / net-up / net-down).

Only the root helper calls these. Same module list, same NetworkManager connection ("gvm-tap",
tap gvmtap0, shared 10.250.0.1/24) as gvm.sh, so the two tools never fight over resources.
"""
import os
import subprocess
import time

from . import config

MODS = ["gunyah_hypercall", "timer", "gunyah_platform_hooks", "gunyah", "gunyah_rsc_mgr", "gunyah_vcpu",
        "gunyah_qcom", "gunyah_irqfd", "gunyah_ioeventfd", "gunyah_qtvm"]
TAP = "gvmtap0"
NM_CON = "gvm-tap"
NET = "10.250.0.1/24"
LEASES = "/var/lib/NetworkManager/dnsmasq-%s.leases" % TAP


class OpError(RuntimeError):
    pass


def _run(argv, timeout=60):
    r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    return r.returncode, (r.stdout + r.stderr).strip()


def loaded_modules():
    try:
        with open("/proc/modules", encoding="ascii") as f:
            return {line.split()[0] for line in f}
    except OSError:
        return set()


TOP_MODS = ["gunyah_rsc_mgr", "gunyah_vcpu", "gunyah_qcom", "gunyah_irqfd", "gunyah_ioeventfd", "gunyah_qtvm"]


def kernel_has_modules():
    """True when the running kernel ships the Gunyah modules (kernel-iceland q90m+, CONFIG_GUNYAH=m)."""
    rc, _ = _run(["modinfo", "-n", "gunyah_rsc_mgr"])
    return rc == 0


def legacy_mods_dir():
    """An iceland-gvm <= 0.3-4 payload with host modules built for exactly this kernel, or None."""
    ko = os.path.join(config.GVM_DIR, "mods", "gunyah.ko")
    if not os.path.exists(ko):
        return None
    rc, out = _run(["modinfo", "-F", "vermagic", ko])
    return os.path.dirname(ko) if rc == 0 and out and out.split()[0] == os.uname().release else None


def load_modules(log=print):
    if os.path.exists("/dev/gunyah"):
        return "already loaded"
    if kernel_has_modules():
        rc, out = _run(["modprobe", "-a"] + TOP_MODS)
        if rc:
            raise OpError("modprobe of the Gunyah modules failed: %s" % out)
        how = "kernel modules"
    else:
        d = legacy_mods_dir()
        if not d:
            raise OpError("this kernel (%s) has no Gunyah modules: install kernel-iceland q90m or later"
                          % os.uname().release)
        have = loaded_modules()
        for m in MODS:
            if m in have:
                continue
            rc, out = _run(["insmod", os.path.join(d, m + ".ko")])
            if rc:
                raise OpError("insmod %s failed: %s" % (m, out))
        how = "iceland-gvm payload modules"
    for _ in range(20):
        if os.path.exists("/dev/gunyah"):
            break
        time.sleep(0.3)
    if not os.path.exists("/dev/gunyah"):
        raise OpError("no /dev/gunyah after loading the modules")
    os.chmod("/dev/gunyah", 0o600)
    log("Gunyah modules loaded (%s), /dev/gunyah present" % how)
    return "loaded"


UNLOAD_KEPT = "kept loaded (pm-notifier bug, fixed in q90o)"


def unload_allowed():
    """gunyah_rsc_mgr never unregisters its PM notifier: after unloading, the next suspend oopses
    (q90m / q90n, fixed in q90o). Until q90o is proven nothing unloads the modules (loading again is
    idempotent); /etc/iceland-vms/allow-unload re-enables it on a fixed kernel."""
    return os.path.exists("/etc/iceland-vms/allow-unload")


def unload_modules(log=None):
    if not unload_allowed():
        if log:
            log("Gunyah modules " + UNLOAD_KEPT)
        return []
    if kernel_has_modules():
        rc, _ = _run(["modprobe", "-r", "-a"] + list(reversed(TOP_MODS)))
        return [m for m in MODS if m in loaded_modules()] if rc else []
    have = loaded_modules()
    failed = []
    for m in reversed(MODS):
        if m in have:
            rc, _ = _run(["rmmod", m])
            if rc:
                failed.append(m)
    return failed


def tap_up():
    return os.path.exists("/sys/class/net/%s" % TAP)


def net_up(log=print):
    rc, out = _run(["nmcli", "-t", "-f", "NAME", "connection", "show"])
    if NM_CON not in out.splitlines():
        rc, out = _run(["nmcli", "connection", "add", "type", "tun", "con-name", NM_CON, "ifname", TAP,
                        "mode", "tap", "owner", "0", "tun.vnet-hdr", "yes", "ipv4.method", "shared",
                        "ipv4.addresses", NET, "ipv6.method", "disabled", "connection.autoconnect", "no"])
        if rc:
            raise OpError("nmcli connection add %s: %s" % (NM_CON, out))
    if not tap_up():
        rc, out = _run(["nmcli", "connection", "up", NM_CON])
        if rc:
            raise OpError("nmcli connection up %s: %s" % (NM_CON, out))
    log("tap %s up (NAT via NetworkManager shared mode)" % TAP)


def net_down():
    _run(["nmcli", "connection", "down", NM_CON])
    _run(["nmcli", "connection", "delete", NM_CON])


def lease_ip(mac=None):
    """Guest IPv4 from NetworkManager's dnsmasq lease file: the newest lease of mac, None while mac has none.
    Without mac: the newest lease of any guest. 0.2-14: no fallback to another MAC's lease (release run WA-4 took
    the stale lease of gvm.sh's Windows MAC at once and probed RDP on an address nobody had)."""
    try:
        with open(LEASES, encoding="ascii") as f:
            rows = [line.split() for line in f if line.strip()]
    except OSError:
        return None
    rows = [r for r in rows if len(r) >= 3]
    if mac:
        rows = [r for r in rows if r[1].lower() == mac.lower()]
    rows.sort(key=lambda r: int(r[0]) if r[0].isdigit() else 0)
    return rows[-1][2] if rows else None


def uplink_zones():
    """firewalld zones of the interfaces that carry a default route (e.g. wlp1s0 in zone home), else the default zone.
    A7 23 Oct: the default zone (FedoraWorkstation) was not the Wi-Fi's zone (home)."""
    zones = []
    for fam in ("-4", "-6"):
        rc, out = _run(["ip", fam, "route", "show", "default"])
        for line in out.splitlines():
            w = line.split()
            if "dev" in w:
                dev = w[w.index("dev") + 1]
                rc, z = _run(["firewall-cmd", "--get-zone-of-interface=" + dev])
                if rc == 0 and z.strip() and z.strip() not in zones:
                    zones.append(z.strip())
    if not zones:
        rc, z = _run(["firewall-cmd", "--get-default-zone"])
        if rc == 0 and z.strip():
            zones.append(z.strip())
    return zones


def open_ports(forwards, add=True):
    """Runtime-only firewalld --add-port for the proxied host ports in every uplink zone. Returns [(zone, spec)]."""
    done = []
    for zone in uplink_zones():
        for f in forwards:
            spec = "%d/%s" % (f["host"], f["proto"])
            rc, _ = _run(["firewall-cmd", "--zone", zone, ("--add-port=" if add else "--remove-port=") + spec])
            if rc == 0:
                done.append((zone, spec))
    return done


def close_ports(done):
    for zone, spec in done:
        _run(["firewall-cmd", "--zone", zone, "--remove-port=" + spec])
