"""GUI-side access to running VMs (as the user): state, privileged actions through pkexec,
the serial console socket, logs, guest shutdown over ssh. No GTK here."""
import json
import os
import socket
import subprocess
import threading

from . import config, storage

STATES = {
    "stopped": "Stopped", "starting": "Starting", "running": "Running", "rebooting": "Rebooting",
    "stopping": "Stopping", "failed": "Failed",
}


def uid():
    return os.getuid()


def home():
    return os.path.expanduser("~")


def unit_state(vmid):
    try:
        r = subprocess.run(["systemctl", "show", "-P", "ActiveState", config.unit_name(uid(), vmid)],
                           capture_output=True, text=True, timeout=5)
        return r.stdout.strip() or "inactive"
    except (OSError, subprocess.TimeoutExpired):
        return "inactive"


def run_state(vmid):
    p = os.path.join(config.run_dir(uid(), vmid), "state.json")
    try:
        with open(p) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def state(vmid):
    """dict(state=..., ip=..., message=...) combining systemd and the supervisor's state file."""
    return combine(unit_state(vmid), run_state(vmid))


def combine(us, rs):
    """The app's state from a unit's ActiveState and the supervisor's state.json contents."""
    if us in ("active", "activating", "reloading"):
        st = rs.get("state", "starting")
        if st in ("stopped", "failed"):
            st = "stopping"
    elif us == "deactivating":
        st = "stopping"
    else:
        st = "failed" if rs.get("state") == "failed" else "stopped"
    return {"state": st, "ip": rs.get("ip") if st == "running" else None, "message": rs.get("message", ""),
            "agent": bool(rs.get("agent")) and st == "running"}


def running_count():
    try:
        r = subprocess.run(["systemctl", "list-units", "--type=service", "--state=active,activating",
                            "--plain", "--no-legend", "iceland-vm-*"], capture_output=True, text=True, timeout=5)
        return len([x for x in r.stdout.splitlines() if x.strip()])
    except (OSError, subprocess.TimeoutExpired):
        return 0


TRANSITIONAL = ("starting", "stopping", "rebooting")


def vm_units():
    """{unit: ActiveState} of every iceland-vm-* unit systemd knows (all users), from ONE systemctl call.
    None when systemctl failed or timed out (the caller keeps what it had)."""
    try:
        r = subprocess.run(["systemctl", "list-units", "--all", "--type=service", "--plain", "--no-legend", "--full",
                            "iceland-vm-*"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    out = {}
    for line in r.stdout.splitlines():
        f = line.split()
        if len(f) >= 3 and f[0].startswith("iceland-vm-"):
            out[f[0]] = f[2]                # UNIT LOAD ACTIVE SUB DESCRIPTION
    return out


def states(vmids, units=None):
    """({vmid: state dict}, running count) for this user's VMs; units from vm_units(). None if it failed."""
    if units is None:
        units = vm_units()
    if units is None:
        return None
    u = uid()
    st = {v: combine(units.get(config.unit_name(u, v), "inactive"), run_state(v)) for v in vmids}
    running = len([a for a in units.values() if a in ("active", "activating")])
    return st, running


class StateMonitor:
    """Polls the VMs' states off the UI thread (0.2-26: the window ran one systemctl per VM every 2 s on its
    main thread, so a slow systemd - a VM starting, memory pressure - froze it). One systemctl per round,
    never two rounds at once; every second while a VM is changing state, else every `interval` seconds.
    on_update(states, running) is called from the worker thread, only when something changed or after kick()."""

    def __init__(self, on_update, interval=3.0, busy_interval=1.0, collect=states):
        self.on_update, self.interval, self.busy_interval, self.collect = on_update, interval, busy_interval, collect
        self.vmids = []
        self.last = None
        self.forced = True
        self.wake = threading.Event()
        self.stopped = False
        self.lock = threading.Lock()
        self.t = None

    def set_vms(self, vmids):
        with self.lock:
            self.vmids = list(vmids)
        self.kick()

    def kick(self):
        """Poll now and report even when nothing changed (after an action, or a new VM list)."""
        self.forced = True
        self.wake.set()

    def start(self):
        if self.t is None:
            self.t = threading.Thread(target=self.loop, name="vm-states", daemon=True)
            self.t.start()

    def stop(self):
        self.stopped = True
        self.wake.set()

    def poll_once(self):
        with self.lock:
            ids = list(self.vmids)
        forced, self.forced = self.forced, False
        res = self.collect(ids)
        if res is None:                     # systemctl failed: keep the last answer
            return self.last
        if forced or res != self.last:
            self.last = res
            self.on_update(*res)
        return res

    def loop(self):
        while not self.stopped:
            self.wake.clear()
            try:
                res = self.poll_once()
            except Exception:  # noqa: BLE001 - never kill the poller
                res = self.last
            busy = res is not None and any(s["state"] in TRANSITIONAL for s in res[0].values())
            self.wake.wait(self.busy_interval if busy else self.interval)


def helper_argv(verb, vmid=None):
    a = ["pkexec", config.HELPER, verb]
    if vmid:
        a.append(vmid)
    return a


def log_paths(vmid):
    d = config.run_dir(uid(), vmid)
    return {"Console": os.path.join(d, "console.log"), "crosvm": os.path.join(d, "crosvm.log"),
            "Supervisor": os.path.join(d, "supervisor.log")}


def read_tail(path, limit=256 * 1024):
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            n = f.tell()
            f.seek(max(0, n - limit))
            return f.read().decode("utf-8", "replace")
    except OSError as e:
        return "(%s)" % e.strerror


class ConsoleClient:
    """Connects to the supervisor's console socket; on_data(bytes) is called from a thread."""

    def __init__(self, vmid, on_data, on_close):
        self.path = os.path.join(config.run_dir(uid(), vmid), "console.sock")
        self.on_data, self.on_close = on_data, on_close
        self.sock = None

    def connect(self):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.connect(self.path)
        self.sock = s
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self):
        while True:
            try:
                d = self.sock.recv(65536)
            except OSError:
                d = b""
            if not d:
                break
            self.on_data(d)
        self.on_close()

    def send(self, data):
        if self.sock:
            try:
                self.sock.sendall(data)
            except OSError:
                pass

    def close(self):
        if self.sock:
            try:
                self.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.sock.close()
            self.sock = None


def ssh_argv(cfg, ip, *cmd):
    key = storage.ssh_key(home())
    return ["ssh", "-i", key, "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null", "-o", "ConnectTimeout=5", "-o", "LogLevel=ERROR",
            "%s@%s" % (cfg["cloud_init"]["user"], ip)] + list(cmd)


def guest_poweroff(cfg, ip):
    """Clean shutdown through ssh (cloud-init VMs carry the app's key). True if accepted."""
    try:
        r = subprocess.run(ssh_argv(cfg, ip, "sudo", "systemctl", "poweroff"), capture_output=True, timeout=20)
        return r.returncode in (0, 255)    # 255: the connection dropped because the guest went down
    except (OSError, subprocess.TimeoutExpired):
        return False


def usb_devices():
    """Host USB devices for the qemu-gunyah passthrough picker: list of dicts."""
    out = []
    base = "/sys/bus/usb/devices"
    try:
        names = sorted(os.listdir(base))
    except OSError:
        return out
    for n in names:
        d = os.path.join(base, n)

        def rd(f):
            try:
                with open(os.path.join(d, f)) as fh:
                    return fh.read().strip()
            except OSError:
                return ""
        v, p = rd("idVendor"), rd("idProduct")
        if not v or rd("bDeviceClass") == "09":         # skip hubs
            continue
        out.append({"vendor": v, "product": p, "name": (rd("manufacturer") + " " + rd("product")).strip() or n,
                    "bus": n})
    return out
