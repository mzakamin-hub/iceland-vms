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
    us = unit_state(vmid)
    rs = run_state(vmid)
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
