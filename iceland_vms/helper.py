"""iceland-vms-helper: the small root part of the VM app (run through pkexec, polkit action
org.iceland.vms.manage). The GUI itself never runs as root.

Verbs (the calling user is PKEXEC_UID; a user can only act on their own VMs):
  start ID       guards, load the Gunyah modules, NAT tap, then a transient systemd unit
                 iceland-vm-u<UID>-<ID>.service that runs "supervise"
  shutdown ID    ask the guest to power off (Linux: the guest agent; Windows: ssh, else the power key)
  stop ID        stop the unit (crosvm stop, then SIGKILL after 15 s) = force off
  net-down       remove the NAT connection (only when no VM runs)
  unload         unload the Gunyah modules: a no-op until q90o (pm-notifier bug; /etc/iceland-vms/allow-unload)
  stop-all       stop every VM (system-sleep hook, shutdown)
  supervise UID ID   (internal; only inside its systemd unit) open the files safely, start crosvm,
                 relay the serial console, restart on guest reboot, battery watchdog, port forwards
"""
import ctypes
import fcntl
import json
import os
import pwd
import select
import signal
import socket
import stat
import subprocess
import sys
import threading
import time

from . import config, crosvmargs, gvmlib, host, limit_malloc_arenas, portproxy, qemuargs

CROSVM = os.path.join(config.GVM_DIR, "crosvm")
KERNEL = os.path.join(config.GVM_DIR, "guest", "Image")
INITRD = os.path.join(config.GVM_DIR, "guest", "initramfs-gvm.cpio.gz")
LOCK = os.path.join(config.RUN_DIR, "helper.lock")


class HelperError(RuntimeError):
    pass


# ---- safe file access ---------------------------------------------------------------------
def open_nofollow(path, flags, owner=None, kind="file"):
    """Open path refusing every symlink on the way; check type and (optionally) the owner."""
    if not path.startswith("/") or "\0" in path:
        raise HelperError("bad path %r" % path)
    parts = [p for p in path.split("/") if p]
    if any(p in (".", "..") for p in parts):
        raise HelperError("bad path %r" % path)
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for i, p in enumerate(parts):
            last = i == len(parts) - 1
            if not last:
                fl = os.O_RDONLY | os.O_DIRECTORY
            elif kind == "dir":
                fl = os.O_RDONLY | os.O_DIRECTORY
            else:
                fl = flags | os.O_NONBLOCK           # never block on a FIFO planted in place of a disk
            nfd = os.open(p, fl | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
            os.close(fd)
            fd = nfd
        st = os.fstat(fd)
        if kind == "file" and not stat.S_ISREG(st.st_mode):
            raise HelperError("%s is not a regular file" % path)
        if kind == "dir" and not stat.S_ISDIR(st.st_mode):
            raise HelperError("%s is not a directory" % path)
        if owner is not None and st.st_uid != owner:
            raise HelperError("%s is not owned by the user" % path)
        if kind == "file":
            fcntl.fcntl(fd, fcntl.F_SETFL, fcntl.fcntl(fd, fcntl.F_GETFL) & ~os.O_NONBLOCK)
        return fd
    except OSError as e:
        os.close(fd)
        raise HelperError("cannot open %s safely: %s" % (path, e.strerror)) from None
    except HelperError:
        os.close(fd)
        raise


def read_user_config(pw, vmid):
    if not config.ID_RE.match(vmid):
        raise HelperError("bad VM id")
    p = os.path.join(config.vm_dir(pw.pw_dir, vmid), "vm.json")
    fd = open_nofollow(p, os.O_RDONLY, owner=pw.pw_uid)
    with os.fdopen(fd, "rb") as f:
        data = f.read(256 * 1024)
    try:
        cfg = config.merged(json.loads(data))
    except ValueError:
        raise HelperError("vm.json is not valid JSON") from None
    if cfg.get("id") != vmid:
        raise HelperError("vm.json id does not match its folder")
    return config.validate(cfg, host.features())


def caller():
    uid = os.environ.get("PKEXEC_UID")
    if os.geteuid() != 0:
        raise HelperError("must run as root (through pkexec)")
    if uid is None or not uid.isdigit():
        raise HelperError("must be started through pkexec (PKEXEC_UID missing)")
    return pwd.getpwuid(int(uid))


# ---- units --------------------------------------------------------------------------------
def systemctl(*args, timeout=30):
    r = subprocess.run(["systemctl"] + list(args), capture_output=True, text=True, timeout=timeout)
    return r.returncode, r.stdout.strip()


def running_units():
    rc, out = systemctl("list-units", "--type=service", "--state=active,activating,deactivating",
                        "--plain", "--no-legend", "iceland-vm-*")
    return [line.split()[0] for line in out.splitlines() if line.strip()]


def other_vmm_running():
    r = subprocess.run(["pgrep", "-x", "crosvm|qemu-system-aar"], capture_output=True, text=True)
    return [p for p in r.stdout.split() if p]


def ensure_run_dir(pw, vmid):
    os.makedirs(config.RUN_DIR, mode=0o755, exist_ok=True)
    u = config.run_dir(pw.pw_uid)
    v = config.run_dir(pw.pw_uid, vmid)
    for d in (u, v):
        if os.path.islink(d):
            raise HelperError("%s is a symlink" % d)
        os.makedirs(d, mode=0o750, exist_ok=True)
        os.chown(d, 0, pw.pw_gid)
        os.chmod(d, 0o750)
    return v


# ---- verbs --------------------------------------------------------------------------------
def verb_start(vmid):
    pw = caller()
    cfg = read_user_config(pw, vmid)
    feats = host.features()
    os.makedirs(config.RUN_DIR, mode=0o755, exist_ok=True)
    with open(LOCK, "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        unit = config.unit_name(pw.pw_uid, vmid)
        units = running_units()
        if unit in units:
            raise HelperError("this VM is already running")
        n = len(units) + (1 if other_vmm_running() and not units else 0)
        why = host.start_guard(cfg, feats, running=n)
        if why:
            raise HelperError("; ".join(why))
        if not os.access(CROSVM, os.X_OK):
            raise HelperError("iceland-gvm is not installed (%s missing)" % CROSVM)
        if config.resolve_backend(cfg, feats) == "qemu" and not os.access(config.QEMU_BIN, os.X_OK):
            raise HelperError("the qemu-gunyah backend is not installed")
        gvmlib.load_modules(log=lambda m: None)
        if cfg["network"]["mode"] == "nat":
            gvmlib.net_up(log=lambda m: None)
        ensure_run_dir(pw, vmid)
        systemctl("reset-failed", unit)
        argv = ["systemd-run", "--unit", unit, "--collect", "--quiet",
                "--description", "Virtual machine %s of %s" % (vmid, pw.pw_name),
                # Windows: the clean shutdown over ssh may take 2 minutes (WinAgent below)
                "-p", "KillMode=mixed", "-p", "TimeoutStopSec=%d" % (180 if config.is_windows(cfg) else 90),
                "-p", "LimitMEMLOCK=infinity",
                "-p", "OOMScoreAdjust=-500",
                sys.executable, "-I", os.path.realpath(sys.argv[0]), "supervise", str(pw.pw_uid), vmid]
        r = subprocess.run(argv, capture_output=True, text=True, timeout=30)
        if r.returncode:
            raise HelperError("systemd-run failed: %s" % r.stderr.strip())
    for _ in range(20):
        time.sleep(0.25)
        rc, st = systemctl("show", "-P", "ActiveState", unit)
        if st in ("active", "failed", "inactive"):
            break
    if st != "active":
        msg = ""
        try:
            with open(os.path.join(config.run_dir(pw.pw_uid, vmid), "state.json")) as f:
                msg = json.load(f).get("message", "")
        except (OSError, ValueError):
            pass
        r = subprocess.run(["journalctl", "-u", unit, "-n", "5", "--no-pager", "-o", "cat"], capture_output=True, text=True)
        why = msg or r.stdout.strip()[-600:] or "no reason recorded"
        raise HelperError("the VM stopped right away: %s (SELinux denials: ausearch -m avc -ts recent)" % why)
    print("started %s" % unit)


def _own_unit(vmid):
    pw = caller()
    if not config.ID_RE.match(vmid):
        raise HelperError("bad VM id")
    return pw, config.unit_name(pw.pw_uid, vmid)


def _signal_unit(unit, sig):
    rc, st = systemctl("show", "-P", "ActiveState", unit)
    if st not in ("active", "activating", "deactivating"):
        raise HelperError("the VM is not running")
    rc, _ = systemctl("kill", "--kill-whom=main", "-s", sig, unit)
    if rc:
        raise HelperError("could not signal %s" % unit)


def verb_shutdown(vmid):
    """Clean shutdown through the guest agent (crosvm powerbtn does nothing for arm64 DT guests)."""
    _pw, unit = _own_unit(vmid)
    _signal_unit(unit, "SIGUSR1")


def verb_force_off(vmid):
    _pw, unit = _own_unit(vmid)
    _signal_unit(unit, "SIGUSR2")


def verb_stop(vmid):
    """Clean shutdown if the guest agent answers (up to 45 s), else crosvm stop; systemd kills after 90 s."""
    _pw, unit = _own_unit(vmid)
    systemctl("stop", unit, timeout=120)


def verb_stop_all():
    if os.geteuid() != 0:
        raise HelperError("must run as root")
    for u in running_units():
        systemctl("stop", u, timeout=120)


def verb_net_down():
    caller()
    if running_units() or other_vmm_running():
        raise HelperError("a VM is running")
    gvmlib.net_down()


def verb_unload():
    caller()
    if running_units() or other_vmm_running():
        raise HelperError("a VM is running")
    if not gvmlib.unload_allowed():
        print("Gunyah modules " + gvmlib.UNLOAD_KEPT)
        return 0
    failed = gvmlib.unload_modules()
    if failed:
        raise HelperError("could not unload %s (they stay until the next reboot)" % " ".join(failed))


# ---- supervisor ---------------------------------------------------------------------------
CLONE_NEWNS = 0x00020000
CLONE_NEWUSER = 0x10000000
MS_REC, MS_PRIVATE, MS_BIND, MS_REMOUNT, MS_RDONLY = 0x4000, 1 << 18, 4096, 32, 1
AT_EMPTY_PATH, AT_RECURSIVE = 0x1000, 0x8000
OPEN_TREE_CLONE, MOVE_MOUNT_F_EMPTY_PATH = 1, 4
MOUNT_ATTR_RDONLY, MOUNT_ATTR_NOSUID, MOUNT_ATTR_NODEV, MOUNT_ATTR_IDMAP = 0x1, 0x2, 0x4, 0x100000
SYS_OPEN_TREE, SYS_MOVE_MOUNT, SYS_MOUNT_SETATTR = 428, 429, 442     # generic syscall table (arm64, x86_64)

_libc = ctypes.CDLL(None, use_errno=True)


def _check(r, what):
    if r < 0 or r is None:
        e = ctypes.get_errno()
        raise HelperError("%s: %s" % (what, os.strerror(e)))
    return r


class _MountAttr(ctypes.Structure):
    _fields_ = [("attr_set", ctypes.c_uint64), ("attr_clr", ctypes.c_uint64),
                ("propagation", ctypes.c_uint64), ("userns_fd", ctypes.c_uint64)]


def _userns_fd(uid, gid):
    """User namespace for an idmapped mount: what crosvm (root) creates is stored as uid/gid on disk,
    the user's files appear as root to crosvm, other owners appear as overflow ids (not writable)."""
    ready_r, ready_w = os.pipe()
    go_r, go_w = os.pipe()
    pid = os.fork()
    if pid == 0:                  # child: enter a new user namespace, report, wait for the maps
        try:
            os.close(ready_r)
            os.close(go_w)
            ok = _libc.unshare(CLONE_NEWUSER) == 0
            os.write(ready_w, b"1" if ok else b"0")
            os.read(go_r, 1)
        finally:
            os._exit(0)
    os.close(ready_w)
    os.close(go_r)
    try:
        if os.read(ready_r, 1) != b"1":
            raise HelperError("cannot create a user namespace for the idmapped share")
        with open("/proc/%d/uid_map" % pid, "w") as f:
            f.write("%d 0 1\n" % uid)          # ns uid <user> = host uid 0 (crosvm)
        with open("/proc/%d/setgroups" % pid, "w") as f:
            f.write("deny")
        with open("/proc/%d/gid_map" % pid, "w") as f:
            f.write("%d 0 1\n" % gid)
        return os.open("/proc/%d/ns/user" % pid, os.O_RDONLY | os.O_CLOEXEC)
    finally:
        os.write(go_w, b"x")
        os.close(go_w)
        os.close(ready_r)
        os.waitpid(pid, 0)


def idmapped_bind(src_fd, target, uid, gid, readonly):
    """Bind src (an fd) to target as an idmapped mount: files the guest creates belong to the user."""
    tree = _check(_libc.syscall(SYS_OPEN_TREE, src_fd, b"", OPEN_TREE_CLONE | AT_EMPTY_PATH | os.O_CLOEXEC),
                  "open_tree")
    ns = _userns_fd(uid, gid)
    try:
        attr = _MountAttr(MOUNT_ATTR_IDMAP | MOUNT_ATTR_NOSUID | MOUNT_ATTR_NODEV | (MOUNT_ATTR_RDONLY if readonly else 0),
                          0, 0, ns)
        _check(_libc.syscall(SYS_MOUNT_SETATTR, tree, b"", AT_EMPTY_PATH, ctypes.byref(attr), ctypes.sizeof(attr)),
               "mount_setattr (idmap)")
        _check(_libc.syscall(SYS_MOVE_MOUNT, tree, b"", -100, target.encode(), MOVE_MOUNT_F_EMPTY_PATH), "move_mount")
    finally:
        os.close(ns)
        os.close(tree)


class Console:
    """Relays crosvm's serial port (unix-stream, crosvm connects) to a log and to GUI clients."""
    SCROLLBACK = 256 * 1024

    def __init__(self, rdir, uid, gid):
        self.inner_path = os.path.join(rdir, "serial.sock")
        self.outer_path = os.path.join(rdir, "console.sock")
        for p in (self.inner_path, self.outer_path):
            if os.path.lexists(p):
                os.unlink(p)
        self.inner = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.inner.bind(self.inner_path)
        os.chmod(self.inner_path, 0o600)
        self.inner.listen(1)
        self.outer = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.outer.bind(self.outer_path)
        os.chown(self.outer_path, uid, gid)
        os.chmod(self.outer_path, 0o600)
        self.outer.listen(4)
        self.log = open(os.path.join(rdir, "console.log"), "ab", buffering=0)
        os.fchown(self.log.fileno(), 0, gid)
        os.fchmod(self.log.fileno(), 0o640)
        self.vm, self.clients, self.buf = None, [], bytearray()
        self.since = bytearray()            # output of the current VMM run (reboot detection)
        self.stop = False
        self.t = threading.Thread(target=self.loop, daemon=True)
        self.t.start()

    def _drop(self, c):
        if c in self.clients:
            self.clients.remove(c)
        c.close()

    def loop(self):
        while not self.stop:
            rl = [self.inner, self.outer] + self.clients + ([self.vm] if self.vm else [])
            try:
                r, _, _ = select.select(rl, [], [], 0.5)
            except (OSError, ValueError):
                continue
            for s in r:
                if s is self.inner:
                    c, _ = self.inner.accept()
                    if self.vm:
                        self.vm.close()
                    self.vm = c
                elif s is self.outer:
                    c, _ = self.outer.accept()
                    c.setblocking(True)
                    try:
                        c.sendall(bytes(self.buf))
                        self.clients.append(c)
                    except OSError:
                        c.close()
                elif s is self.vm:
                    try:
                        d = s.recv(65536)
                    except OSError:
                        d = b""
                    if not d:
                        self.vm.close()
                        self.vm = None
                        continue
                    self.log.write(d)
                    self.buf += d
                    self.since += d
                    if len(self.since) > 65536:
                        del self.since[:len(self.since) - 65536]
                    if len(self.buf) > self.SCROLLBACK:
                        del self.buf[:len(self.buf) - self.SCROLLBACK]
                    for c in list(self.clients):
                        try:
                            c.sendall(d)
                        except OSError:
                            self._drop(c)
                else:
                    try:
                        d = s.recv(4096)
                    except OSError:
                        d = b""
                    if not d:
                        self._drop(s)
                    elif self.vm:
                        try:
                            self.vm.sendall(d)
                        except OSError:
                            pass

    def guest_rebooted(self):
        """True if the guest's last 'reboot:' line of this run says Restarting (kernel pr_emerg)."""
        i = self.since.rfind(b"reboot: ")
        return i >= 0 and self.since[i:i + 40].startswith(b"reboot: Restarting system")

    def wait_connected(self, timeout):
        t = time.time() + timeout
        while time.time() < t and self.vm is None:
            time.sleep(0.05)
        return self.vm is not None

    def close(self):
        self.stop = True
        self.t.join(2)
        for s in [self.inner, self.outer] + self.clients + ([self.vm] if self.vm else []):
            s.close()
        for p in (self.inner_path, self.outer_path):
            if os.path.lexists(p):
                os.unlink(p)


def wayland_socket(uid):
    """The user's Wayland socket: GNOME (Mutter) and Plasma (kwin_wayland) both normally create
    wayland-0 in /run/user/UID; kwin takes the next free name (wayland-1, ...) when 0 is in use.
    Only a socket owned by the user is accepted (never another user's or a symlink)."""
    rd = "/run/user/%d" % uid
    try:
        names = os.listdir(rd)
    except OSError:
        names = []
    cands = sorted((n for n in names if n.startswith("wayland-") and n[8:].isdigit()), key=lambda n: int(n[8:]))
    for n in cands:
        p = os.path.join(rd, n)
        try:
            st = os.lstat(p)
        except OSError:
            continue
        if stat.S_ISSOCK(st.st_mode) and st.st_uid == uid:
            return p
    raise HelperError("no graphical session for the display window (no Wayland socket of uid %d in %s); "
                      "log in first or set display to none" % (uid, rd))


# ---- Windows guests: ssh identities, the one-time app-key install, the power key (0.2-21, WA-7) --------------
# OpenSSH's client adds its default identities (~/.ssh/id_*) ONLY when no -i is given: 0.2-12..0.2-20 passed the app's
# key with -i and so never offered the owner's key, which is the one the Windows image was built with (the image's
# administrators_authorized_keys = the owner's tablet key, win-image-build.ps1 -SshPubKey). The final run's WA-7 failed
# with "Permission denied (publickey)" on every attempt for exactly this reason (gvm.sh's W4-3 shutdown, which runs ssh
# without -i, powered the same image off).
OWNER_KEYS = ("id_ed25519", "id_ecdsa", "id_ed25519_sk", "id_ecdsa_sk", "id_rsa")
WIN_KEY_OK = "ICELAND-VMS-KEY-OK"
KEY_POWER, EV_KEY, EV_SYN = 116, 1, 0


def win_identities(home, app_key):
    """(app key or None, [the owner's default private keys that exist]) - the order ssh offers them in."""
    app = app_key if app_key and os.path.isfile(app_key) else None
    own = [p for p in (os.path.join(home, ".ssh", n) for n in OWNER_KEYS) if os.path.isfile(p)]
    return app, own


def win_ssh_argv(identities, user, ip, known_hosts, cmd):
    """ssh argv offering exactly identities (IdentitiesOnly: the order is ours, no agent keys)."""
    argv = ["ssh", "-o", "BatchMode=yes", "-o", "PasswordAuthentication=no", "-o", "ConnectTimeout=5",
            "-o", "StrictHostKeyChecking=accept-new", "-o", "UserKnownHostsFile=" + known_hosts, "-o", "LogLevel=ERROR",
            "-o", "IdentitiesOnly=yes"]
    for k in identities:
        argv += ["-i", k]
    return argv + ["%s@%s" % (user, ip), cmd]


def win_key_install_cmd(pubkey):
    """One PowerShell command (run over ssh with the owner's key) that adds the app's public key to
    C:\\ProgramData\\ssh\\administrators_authorized_keys if missing, restores the ACL OpenSSH requires
    (Administrators + SYSTEM only) and sets the power button to "Shut down" (the app's fallback presses it)."""
    import base64
    lines = [ln.strip() for ln in pubkey.strip().splitlines() if ln.strip()]
    key = lines[0] if lines else ""
    if not key.startswith(("ssh-ed25519 ", "ecdsa-sha2-", "ssh-rsa ", "sk-")) or "'" in key or len(lines) != 1:
        raise HelperError("not a single OpenSSH public key")
    ps = ("$ErrorActionPreference='Stop'; $f='C:\\ProgramData\\ssh\\administrators_authorized_keys'; $k='%s'; "
          "$c=''; if (Test-Path $f) { $c=[IO.File]::ReadAllText($f) }; "
          "if (-not $c.Contains($k)) { if ($c.Length -gt 0 -and -not $c.EndsWith([string][char]10)) "
          "{ $c+=[string][char]13+[char]10 }; [IO.File]::WriteAllText($f, $c+$k+[string][char]13+[char]10) }; "
          "icacls $f /inheritance:r /grant '*S-1-5-32-544:F' /grant 'SYSTEM:F' | Out-Null; "
          "powercfg /setacvalueindex SCHEME_CURRENT SUB_BUTTONS PBUTTONACTION 3; "
          "powercfg /setdcvalueindex SCHEME_CURRENT SUB_BUTTONS PBUTTONACTION 3; powercfg /setactive SCHEME_CURRENT; "
          "'%s'" % (key, WIN_KEY_OK))
    return "powershell -NoProfile -NonInteractive -EncodedCommand " + base64.b64encode(ps.encode("utf-16-le")).decode()


def virtio_key_tap(code=KEY_POWER):
    """virtio_input_event records (le16 type, le16 code, le32 value) for one key press + release, as crosvm's
    --input keyboard[path=] socket reads them."""
    import struct

    def ev(t, c, v):
        return struct.pack("<HHi", t, c, v)
    return ev(EV_KEY, code, 1) + ev(EV_SYN, 0, 0) + ev(EV_KEY, code, 0) + ev(EV_SYN, 0, 0)


AGENT_REFRESH_CMD = (
    "sudo -n sh -c 'mkdir -p /mnt/gvmshare; mountpoint -q /mnt/gvmshare || "
    "mount -t 9p -o trans=virtio,version=9p2000.L,ro gvmshare /mnt/gvmshare; a=/mnt/gvmshare/gvm-agent; "
    "[ -f $a ] || { echo AGENT-NOSHARE; exit 3; }; "
    "cmp -s $a /usr/local/sbin/gvm-agent && { echo AGENT-CURRENT; exit 0; }; "
    "install -m 755 $a /usr/local/sbin/gvm-agent && echo AGENT-UPDATED && "
    "GVM_AGENT_UPDATED=1 /usr/local/sbin/gvm-agent --refresh 2>&1 | tail -n 4'")


class AgentRefresh:
    """0.2-24: guests whose gvm-agent predates the share's (before 0.2-23 an agent could not update itself, and before
    0.2-24 it read its flags from tools/ of the share, where the app never put them) get the app's agent once ssh
    answers: installed over ssh with the app's key AS THE VM's OWNER (cloud-init user, passwordless sudo), then
    `gvm-agent --refresh` applies the flags now (autologin, noglamor, wayland, zink, native) and restarts the display
    manager when its configuration changed and no desktop session is open. Agents >= 0.2-23 update themselves at boot,
    so this normally logs "up to date"."""
    CAP = 600

    def __init__(self, sup):
        self.sup = sup
        threading.Thread(target=self.loop, name="agent-refresh", daemon=True).start()

    def ip(self):
        return self.sup.state.get("ip") or gvmlib.lease_ip(self.sup.cfg["network"]["mac"])

    def ssh(self, cmd, timeout=90):
        pw = self.sup.pw
        argv = ["ssh", "-i", os.path.join(config.data_home(pw.pw_dir), "ssh", "id_ed25519"), "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no",
                "-o", "UserKnownHostsFile=/dev/null", "-o", "ConnectTimeout=5", "-o", "LogLevel=ERROR",
                "-o", "IdentitiesOnly=yes", "%s@%s" % (self.sup.cfg["cloud_init"]["user"], self.ip()), cmd]
        env = {"HOME": pw.pw_dir, "USER": pw.pw_name, "LOGNAME": pw.pw_name, "PATH": "/usr/bin:/bin"}
        try:
            r = subprocess.run(argv, user=pw.pw_uid, group=pw.pw_gid, extra_groups=[], cwd=pw.pw_dir, env=env,
                               stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout,
                               start_new_session=True, close_fds=True)
            return r.returncode, (r.stdout or "").strip(), (r.stderr or "").strip().replace(chr(10), " ")[:200]
        except (OSError, subprocess.TimeoutExpired) as e:
            return -1, "", str(e)[:200]

    def loop(self):
        t0, tries = time.time(), 0
        while time.time() - t0 < self.CAP:
            if self.sup.state.get("state") in ("stopping", "stopped", "failed"):
                return
            ip = self.ip()
            up = False
            if ip:
                try:
                    with socket.create_connection((ip, 22), timeout=2):
                        up = True
                except OSError:
                    up = False
            if up:
                tries += 1
                rc, out, err = self.ssh(AGENT_REFRESH_CMD)
                if rc == 0 and "AGENT-CURRENT" in out:
                    self.sup.log("guest agent: up to date")
                    return
                if rc == 0 and "AGENT-UPDATED" in out:
                    self.sup.log("guest agent: updated over ssh and applied: %s"
                                 % " | ".join(ln for ln in out.splitlines() if ln and ln != "AGENT-UPDATED")[-300:])
                    return
                if "AGENT-NOSHARE" in out or tries >= 4:
                    self.sup.log("guest agent: not refreshed (rc %d): %s" % (rc, (out or err or "-")[-200:]))
                    return
                time.sleep(10)          # cloud-init may still be setting up the user / sudo
            else:
                time.sleep(3)


class WinAgent:
    """0.2-12: the clean-shutdown "agent" of a Windows guest (no virtio-console driver in Windows).

    alive = the guest's OpenSSH answers on port 22 of its DHCP address (polled in a thread); "poweroff" runs
    ssh <windows.ssh_user>@<ip> shutdown /s /t 0 AS THE VM's OWNER (never root), key only (BatchMode). Windows then
    powers off through PSCI SYSTEM_OFF and crosvm exits (vm-run W4-3).
    0.2-21 (WA-7): the app's key AND the owner's default keys are offered (-i for each; before, -i disabled the default
    keys); the first time ssh answers, the app installs its own key in the guest with the owner's key (once, logged),
    so later shutdowns need only the app's key (kit-vm21 win: W-1..W-5 PASS). If ssh is refused or does not answer,
    the user is told at once to shut Windows down inside it or Force Off; the virtio-keyboard power key
    (windows.power_key) is opt-in and experimental: Windows ignored it in kit-vm21 winpk (WP-5).
    """
    path = None
    GRACE = 120                    # Windows may install updates on the way down

    def __init__(self, sup):
        self.sup, self.alive, self.stop = sup, False, False
        self.pk = None                 # our end of the power-key socket (set by the supervisor on crosvm)
        self.provisioned = False
        self.t = threading.Thread(target=self.loop, daemon=True)
        self.t.start()

    def ip(self):
        return self.sup.state.get("ip") or gvmlib.lease_ip(self.sup.cfg["network"]["mac"])

    def loop(self):
        while not self.stop:
            ip = self.ip() if self.sup.cfg["network"]["mode"] == "nat" else None
            ok = False
            if ip:
                try:
                    with socket.create_connection((ip, 22), timeout=2):
                        ok = True
                except OSError:
                    ok = False
            if ok != self.alive:
                self.alive = ok
                self.sup.log("windows: ssh on %s %s" % (ip, "answers" if ok else "does not answer"))
                if ok and not self.provisioned:
                    self.provisioned = True
                    threading.Thread(target=self.provision, name="win-key", daemon=True).start()
            self.drain()
            for _ in range(10):
                if self.stop:
                    break
                time.sleep(0.5)

    # -- ssh as the VM's owner -----------------------------------------------------------------------------------
    def keys(self):
        pw = self.sup.pw
        return win_identities(pw.pw_dir, os.path.join(config.data_home(pw.pw_dir), "ssh", "id_ed25519"))

    def ssh(self, identities, cmd, timeout=60):
        pw = self.sup.pw
        kh = os.path.join(config.vm_dir(pw.pw_dir, self.sup.vmid), "known_hosts")
        argv = win_ssh_argv(identities, self.sup.cfg["windows"]["ssh_user"], self.ip(), kh, cmd)
        env = {"HOME": pw.pw_dir, "USER": pw.pw_name, "LOGNAME": pw.pw_name, "PATH": "/usr/bin:/bin"}
        try:
            r = subprocess.run(argv, user=pw.pw_uid, group=pw.pw_gid, extra_groups=[], cwd=pw.pw_dir, env=env,
                               stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout,
                               start_new_session=True, close_fds=True)
            return r.returncode, (r.stdout or ""), (r.stderr or "").strip().replace(chr(10), " ")[:200]
        except (OSError, subprocess.TimeoutExpired) as e:
            return -1, "", str(e)[:200]

    def provision(self):
        """Once per start, when ssh first answers: make the app's own key work (install it with the owner's key)."""
        app, own = self.keys()
        if not app and not own:
            self.sup.log("windows: no ssh key (neither the app's nor ~/.ssh/id_*): Shut Down will press the power key")
            return
        for attempt in range(1, 4):        # a fresh image's OpenSSH task may still be writing its key file
            if self.stop or not self.alive:
                return
            rc, err = -1, "no app key"
            if app:
                rc, _, err = self.ssh([app], "exit", 30)
                if rc == 0:
                    self.sup.log("windows: the app's ssh key is accepted")
                    return
            if app and own:
                try:
                    with open(app + ".pub") as f:
                        cmd = win_key_install_cmd(f.read())
                except (OSError, HelperError) as e:
                    self.sup.log("windows: cannot read the app's public key (%s); the owner's key stays in use" % e)
                    return
                rc, out, err = self.ssh(own, cmd, 90)
                if rc == 0 and WIN_KEY_OK in out:
                    self.sup.log("windows: installed the app's ssh key in the guest with the owner's key "
                                 "(administrators_authorized_keys; power button = shut down)")
                    rc2, _, err2 = self.ssh([app], "exit", 30)
                    self.sup.log("windows: the app's ssh key is %s" % ("accepted now" if rc2 == 0 else
                                                                       "still refused: %s" % err2))
                    return
                self.sup.log("windows: key install attempt %d with the owner's key failed (rc %d): %s"
                             % (attempt, rc, err or out.strip()[-120:] or "-"))
            else:
                self.sup.log("windows: the app's key is refused (rc %d: %s) and there is no owner key to install it"
                             % (rc, err or "-"))
                return
            time.sleep(20)

    # -- the power key (crosvm virtio keyboard) --------------------------------------------------------------------
    def drain(self):
        """Read and drop the LED / status events crosvm writes back on the power-key socket."""
        if not self.pk:
            return
        try:
            while self.pk.recv(4096, socket.MSG_DONTWAIT):
                pass
        except OSError:
            pass

    FORCE_PROMPT = "Windows did not accept the shutdown (%s): shut it down inside Windows, or use Force Off"

    def power_key(self, why):
        """The clean shutdown failed. kit-vm21 winpk: Windows ignores the virtio keyboard's KEY_POWER (vioinput sends
        it as Consumer Control 0x30 "Power", which Windows' power policy does not act on - only the System Control
        "System Power Down" usage would), so the key is opt-in (windows.power_key) and the user is told at once to
        Force Off; a system stop / sleep hook forces the VM off at once instead of waiting out the grace time."""
        pressed = False
        if self.pk:
            try:
                self.pk.sendall(virtio_key_tap())
                pressed = True
                self.sup.log("windows: %s: pressed the VM's power key (KEY_POWER, experimental)" % why)
            except OSError as e:
                self.sup.log("windows: %s; the power key failed (%s)" % (why, e))
        self.sup.log("windows: %s; shut it down inside Windows or use Force Off" % why)
        try:
            self.sup.set_state(self.sup.state.get("state", "running"), self.FORCE_PROMPT % why)
        except (OSError, AttributeError, TypeError):
            pass
        if getattr(self.sup, "term", False):
            self.sup.force_at = time.time()          # stop / sleep hook: no 120 s grace for a refused shutdown
        return pressed

    def send(self, cmd):
        if cmd == "ping":
            return self.alive or bool(self.pk)
        if cmd != "poweroff":
            return False
        if not self.alive:
            self.power_key("ssh does not answer")
            return bool(self.pk)
        ip = self.ip()
        app, own = self.keys()
        ids = ([app] if app else []) + own

        def run():
            # 0.2-15 (fix run WA-7: "asked" was logged but Windows never powered off): wait for ssh's answer and
            # log it; retry while it fails (right after the first boot the OpenSSH task may still be installing the
            # key), stop as soon as the guest is gone. 0.2-21: two refusals of every key -> the power key
            denied = 0
            for attempt in range(1, 9):
                rc, out, err = self.ssh(ids, "shutdown /s /t 0") if ids else (-1, "", "no ssh key")
                # Windows may drop the connection while it starts shutting down: 0 and 255 after a reset both count
                if rc == 0 or (rc == 255 and "closed" in err.lower()):
                    self.sup.log("windows: shutdown /s accepted (ssh rc %d, attempt %d)" % (rc, attempt))
                    # fix2: Windows took the request ~25 s after boot and kept running; if ssh still answers
                    # 90 s later, ask again (at most 3 accepted requests in all)
                    accepted = getattr(self, "accepted", 0) + 1
                    self.accepted = accepted
                    for _ in range(18):
                        if self.stop or not self.alive:
                            return
                        time.sleep(5)
                    if accepted >= 3:
                        self.sup.log("windows: still running 90 s after 3 accepted shutdown requests")
                        self.power_key("shutdown /s did not power off")
                        return
                    self.sup.log("windows: still running 90 s after shutdown /s: asking again")
                    continue
                self.sup.log("windows: ssh shutdown attempt %d failed (rc %d, %d key(s) offered): %s"
                             % (attempt, rc, len(ids), err or "-"))
                if self.stop or not self.alive:
                    return
                if not ids or "Permission denied" in err:
                    denied += 1
                    if denied >= 2 or not ids:
                        self.power_key("the guest refused every ssh key")
                        return
                time.sleep(10)
            self.power_key("the guest did not accept shutdown over ssh")

        threading.Thread(target=run, name="win-shutdown", daemon=True).start()
        self.sup.log("windows: asked the guest to shut down (ssh %s@%s, %d key(s))"
                     % (self.sup.cfg["windows"]["ssh_user"], ip, len(ids)))
        return True

    def close(self):
        self.stop = True
        self.t.join(6)
        if self.pk:
            self.pk.close()


class Agent:
    """Line channel to the guest agent on virtio-console hvc0 (crosvm connects to agent.sock).

    The guest side (gvm-agent, installed by cloud-init or by hand from the tools share) answers
    "ping" with "pong" and runs "poweroff" / "reboot". Clean shutdown needs it: crosvm's
    powerbtn does nothing for aarch64 device-tree guests.
    """

    def __init__(self, rdir, log):
        self.path = os.path.join(rdir, "agent.sock")
        if os.path.lexists(self.path):
            os.unlink(self.path)
        self.lsock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.lsock.bind(self.path)
        os.chmod(self.path, 0o600)
        self.lsock.listen(1)
        self.conn, self.alive, self.log, self.stop = None, False, log, False
        self.t = threading.Thread(target=self.loop, daemon=True)
        self.t.start()

    def loop(self):
        buf = b""
        while not self.stop:
            rl = [self.lsock] + ([self.conn] if self.conn else [])
            try:
                r, _, _ = select.select(rl, [], [], 0.5)
            except (OSError, ValueError):
                continue
            for s in r:
                if s is self.lsock:
                    c, _ = self.lsock.accept()
                    if self.conn:
                        self.conn.close()
                    self.conn, self.alive, buf = c, False, b""
                else:
                    try:
                        d = s.recv(4096)
                    except OSError:
                        d = b""
                    if not d:
                        self.conn.close()
                        self.conn, self.alive = None, False
                        continue
                    buf += d
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        line = line.strip().decode("utf-8", "replace")
                        if line == "pong" and not self.alive:
                            self.alive = True
                            self.log("guest agent answered")
                        elif line:
                            self.log("agent: %s" % line[:200])

    def send(self, cmd):
        if self.conn:
            try:
                self.conn.sendall(cmd.encode() + b"\n")
                return True
            except OSError:
                pass
        return False

    def close(self):
        self.stop = True
        self.t.join(2)
        for s in [self.lsock] + ([self.conn] if self.conn else []):
            s.close()
        if os.path.lexists(self.path):
            os.unlink(self.path)


class Supervisor:
    def __init__(self, uid, vmid):
        self.pw = pwd.getpwuid(uid)
        self.uid, self.gid, self.vmid = uid, self.pw.pw_gid, vmid
        self.cfg = read_user_config(self.pw, vmid)
        self.feats = host.features()
        self.backend = config.resolve_backend(self.cfg, self.feats)
        self.rdir = ensure_run_dir(self.pw, vmid)
        self.logf = open(os.path.join(self.rdir, "supervisor.log"), "a", buffering=1)
        os.fchown(self.logf.fileno(), 0, self.gid)
        os.fchmod(self.logf.fileno(), 0o640)
        self.state = {"state": "starting", "since": int(time.time()), "pid": None, "ip": None, "message": ""}
        self.fds, self.proc, self.term, self.term_at, self.forwards = [], None, False, 0.0, []
        self.hp_restore = None
        self.proxies, self.fw_open, self.shutdown_pending, self.last_ping = [], [], 0.0, 0.0
        self.inhibit = None

    def log(self, msg):
        self.logf.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))

    def set_state(self, st, msg=None):
        self.state["state"] = st
        if msg is not None:
            self.state["message"] = msg
        p = os.path.join(self.rdir, "state.json")
        tmp = p + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.state, f)
        os.chown(tmp, 0, self.gid)
        os.chmod(tmp, 0o640)
        os.replace(tmp, p)
        self.log("state %s %s" % (st, msg or ""))

    def fdpath(self, fd):
        os.set_inheritable(fd, True)
        self.fds.append(fd)
        return "/proc/self/fd/%d" % fd

    def user_file(self, store, name, writable):
        p = os.path.join(config.store_dir(self.pw.pw_dir, store, self.vmid), name)
        return self.fdpath(open_nofollow(p, os.O_RDWR if writable else os.O_RDONLY, owner=self.uid))

    def prepare(self):
        c = self.cfg
        res = {"control_sock": os.path.join(self.rdir, "crosvm.sock"),
               "serial_sock": os.path.join(self.rdir, "serial.sock"),
               "kernel": KERNEL, "initrd": INITRD,
               "firmware": config.firmware_path(c),
               "cpu_sets": host.cpu_sets(), "disks": [], "shares": []}
        if self.backend == "qemu":
            res["firmware"] = res["qemu_firmware"] = qemuargs.QEMU_FIRMWARE
        if c["boot"]["mode"] == "uefi" and not os.path.exists(res["firmware"]):
            raise HelperError("UEFI firmware %s is missing" % res["firmware"])
        for d in c["disks"]:
            res["disks"].append(self.user_file(d["store"], d["name"], not d["ro"]))
        if c["cloud_init"]["enabled"]:
            res["seed"] = self.user_file("vm", c["cloud_init"]["seed"], False)
        if c["guest_tools"]:
            res["tools_dir"] = self.tools_dir()
        if c["network"]["mode"] == "nat":
            if not gvmlib.tap_up():
                gvmlib.net_up(log=self.log)
            res["tap"] = gvmlib.TAP
            if c["network"].get("vhost_net") and self.backend != "qemu":
                if not os.path.exists("/dev/vhost-net"):
                    subprocess.run(["modprobe", "vhost_net"], stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, check=False)
                if not os.path.exists("/dev/vhost-net"):
                    # 0.2-15: on by default now, so a kernel without CONFIG_VHOST_NET falls back instead of failing
                    self.log("vhost-net: this kernel has no /dev/vhost-net; using crosvm's own network")
                    c["network"]["vhost_net"] = False
        if c["display"]["gpu"] != "none" and not c["display"].get("vnc"):
            res["wayland_sock"] = wayland_socket(self.uid)
        if c["display"].get("vnc") and c["display"].get("vnc_password"):
            res["vnc_password_file"] = self.user_file("vm", config.VNC_PASSWORD_FILE, False)
        if c["shares"]:
            res["shares"] = self.shares()
        need = qemuargs.hugepages_needed(c) if self.backend == "qemu" else 0
        if need:                            # QEMU: hugetlb memfd -> reserve 2 MiB pages, released at exit
            with open("/proc/sys/vm/nr_hugepages") as fh:
                self.hp_restore = int(fh.read())
            with open("/proc/sys/vm/nr_hugepages", "w") as fh:
                fh.write(str(self.hp_restore + need))
            free = host.meminfo_raw().get("HugePages_Free", 0)
            if free < need:
                self.release_hugepages()
                raise HelperError("could reserve only %d of %d huge pages (memory too fragmented): turn huge pages "
                                  "off or reboot first" % (free, need))
            self.log("hugetlb: %d x 2 MiB reserved" % need)
        if c.get("hugepages") and self.backend == "crosvm":   # crosvm --hugepages = MADV_HUGEPAGE on the shmem memfd
            f = "/sys/kernel/mm/transparent_hugepage/shmem_enabled"
            try:
                with open(f) as fh:
                    cur = fh.read()
                if "[never]" in cur or "[deny]" in cur:
                    with open(f, "w") as fh:
                        fh.write("advise")
                    self.log("thp: shmem_enabled set to advise (runtime; a reboot restores it)")
                with open("/proc/sys/vm/compact_memory", "w") as fh:
                    fh.write("1")
            except OSError as e:
                self.log("thp: %s" % e)
        if os.path.lexists(res["control_sock"]):
            os.unlink(res["control_sock"])
        return res

    def tools_dir(self):
        d = os.path.join(self.rdir, "tools")
        subprocess.run(["rm", "-rf", "--one-file-system", d], check=False)
        os.makedirs(os.path.join(d, "extra"), mode=0o755)
        t = host.guest_modules_tarball()
        if t:
            subprocess.run(["cp", t, d + "/"], check=False)
        subprocess.run(["sh", "-c", 'cp "$0"/guest/extra/*.ko "$1"/extra/ 2>/dev/null; '
                        'cp "$2"/guest-tools/* "$1"/ 2>/dev/null; cp "$0"/guest/Image "$1"/ 2>/dev/null; true',
                        config.GVM_DIR, d, config.SHARE_DIR], check=False)
        dsp = self.cfg["display"]
        for flag, on in (("noglamor", not dsp.get("glamor", False)),
                         ("autologin", dsp["gpu"] != "none" and dsp.get("autologin", True)),
                         ("wayland", dsp["gpu"] in ("virgl", "venus", "native", "zink") and dsp.get("session", "x11") == "wayland"),
                         ("zink", dsp["gpu"] == "zink"),
                         ("native", dsp["gpu"] == "native")):
            if on:
                open(os.path.join(d, flag), "w").close()
        if dsp["gpu"] == "native":
            # 0.2-23: Fedora's guest Mesa has freedreno without its virtio KMD, so the msm native context fell back to
            # llvmpipe (kit-vm22 N2: fd_device_new() -> NULL). The guest agent unpacks this Mesa under
            # /usr/lib64/iceland-nctx and points the desktop at it while the VM runs in this mode.
            t = config.guest_mesa_tarball()
            if t:
                subprocess.run(["cp", t, d + "/"], check=False)
            else:
                self.log("native context: iceland-vms-guest-mesa is not installed; the guest's own Mesa will fall "
                         "back to llvmpipe")
        return d

    def shares(self):
        """Private mount namespace with one idmapped bind per share (read-only unless asked)."""
        _check(_libc.unshare(CLONE_NEWNS), "unshare(mount namespace)")
        _check(_libc.mount(b"none", b"/", None, MS_REC | MS_PRIVATE, None), "make / private")
        out = []
        allowed = (self.pw.pw_dir.rstrip("/") + "/", "/run/media/%s/" % self.pw.pw_name)
        for sh in self.cfg["shares"]:
            if not (sh["path"] + "/").startswith(allowed):
                raise HelperError("share %s must be inside your home folder or /run/media/%s"
                                  % (sh["path"], self.pw.pw_name))
            fd = open_nofollow(sh["path"], os.O_RDONLY, owner=self.uid, kind="dir")
            mp = os.path.join(self.rdir, "share-" + sh["tag"])
            os.makedirs(mp, mode=0o700, exist_ok=True)
            try:
                idmapped_bind(fd, mp, self.uid, self.gid, sh["ro"])
            finally:
                os.close(fd)
            out.append((mp, sh["tag"], sh["kind"]))
            self.log("share %s -> %s (%s, %s)" % (sh["path"], sh["tag"], sh["kind"], "ro" if sh["ro"] else "rw"))
        return out

    def release_hugepages(self):
        if self.hp_restore is not None:
            try:
                with open("/proc/sys/vm/nr_hugepages", "w") as fh:
                    fh.write(str(self.hp_restore))
            except OSError as e:
                self.log("hugetlb release: %s" % e)
            self.hp_restore = None

    def gvm_exit_reset(self):
        """True when crosvm's last GVM-EXIT line of this run reports a PSCI system reset."""
        try:
            with open(os.path.join(self.rdir, "crosvm.log"), "rb") as f:
                f.seek(0, os.SEEK_END)
                f.seek(max(0, f.tell() - 65536))
                tail = f.read().decode("utf-8", "replace")
        except OSError:
            return False
        exits = [ln for ln in tail.splitlines() if "GVM-EXIT type=" in ln]
        return bool(exits) and ("type=2 " in exits[-1] or "type=3 " in exits[-1])

    DISPLAY_LOST = ("connection to compositor has been broken", "Display processing failed")

    def display_watch(self):
        """Tell the user when crosvm lost its Wayland window (23 Oct: Mutter 'Not Responding', frozen window)."""
        p = os.path.join(self.rdir, "crosvm.log")
        try:
            with open(p, "rb") as f:
                if not hasattr(self, "_log_off"):
                    # 0.2-22 (kit-vm21 Q-1): the log is appended across starts; an earlier start's
                    # "connection to compositor has been broken" must not mark this start's window lost
                    f.seek(0, os.SEEK_END)              # normally set by run() before the first boot
                    self._log_off = f.tell()
                f.seek(self._log_off)
                new = f.read().decode("utf-8", "replace")
                self._log_off = f.tell()
        except OSError:
            return
        if "GVM-WINDOW-CLOSE" in new and not self.term:
            # crosvm 0.3-10: the compositor's close (KWin title-bar X, Alt+F4) no longer exits the VM
            if self.agent and self.agent.alive:
                self.log("display window closed: clean guest shutdown")
                self.set_state("stopping", "Window closed: shutting the guest down")
                self.on_shutdown()
            else:
                self.log("display window close ignored: no guest agent")
                self.set_state(self.state["state"], "Closing the window does not stop this VM (no guest agent): "
                               "use Shut Down or Force Off")
        if self.cfg["display"]["gpu"] != "none" and not self.cfg["display"].get("vnc") and \
                any(m in new for m in self.DISPLAY_LOST) and not self.state.get("display_lost"):
            self.state["display_lost"] = True
            self.log("display window lost: " + next(m for m in self.DISPLAY_LOST if m in new))
            self.set_state(self.state["state"], "The display window was lost (the guest keeps running): "
                           "use Shut Down, or reach the guest by ssh / the console")

    def watchdog(self):
        self.display_watch()
        cap, on, _ = host.power()
        if cap is not None and not on and cap <= self.feats["stop_battery_percent"] and not self.term:
            self.log("battery %d %% without charger: shutting the guest down" % cap)
            self.set_state("stopping", "Battery low (%d %%): shutting down" % cap)
            self.term = True
            if self.guest_poweroff():
                g = getattr(self.agent, "GRACE", 45)
                self.term_at = time.time() + g + 15
                self.force_at = time.time() + g
            else:
                self.crosvm_stop(15)
        if self.cfg["network"]["mode"] == "nat" and not self.state["ip"]:
            ip = gvmlib.lease_ip(self.cfg["network"]["mac"])
            if ip:
                self.state["ip"] = ip
                if self.cfg["network"]["forwards"]:
                    self.stop_forwards()
                    self.proxies = portproxy.start(self.cfg["network"]["forwards"], ip, self.log)
                    self.fw_open = gvmlib.open_ports(self.cfg["network"]["forwards"])
                    self.log("port forwards open in zones: %s" % sorted({z for z, _ in self.fw_open}))
                self.set_state(self.state["state"])

    def stop_forwards(self):
        portproxy.stop(self.proxies)
        gvmlib.close_ports(self.fw_open)
        self.proxies, self.fw_open = [], []

    def crosvm_stop(self, grace):
        """Immediate VM exit (crosvm stop on its socket; QEMU exits cleanly on SIGTERM)."""
        if self.backend == "qemu":
            if self.proc and self.proc.poll() is None:
                self.proc.terminate()
        else:
            subprocess.Popen([CROSVM, "stop", self.res["control_sock"]], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        self.term_at = min(self.term_at or time.time() + grace, time.time() + grace)

    def guest_poweroff(self):
        """Clean shutdown through the guest agent; False if no agent answers."""
        if self.agent and self.agent.alive and self.agent.send("poweroff"):
            self.log("asked the guest agent to power off")
            return True
        return False

    def on_shutdown(self, *_):          # SIGUSR1: the user's "Shut Down"
        if not self.guest_poweroff():
            self.shutdown_pending = time.time()          # the main loop pings and retries for 20 s
            self.agent.send("ping")

    def on_force(self, *_):             # SIGUSR2: "Force Off"
        self.term = True
        self.set_state("stopping", "Forcing off")
        self.crosvm_stop(15)

    def on_term(self, *_):              # systemctl stop (also the sleep hook): clean first, then force
        if self.term:
            return
        self.term = True
        if self.guest_poweroff():
            g = getattr(self.agent, "GRACE", 45)
            self.set_state("stopping", "Shutting down the guest")
            self.term_at = time.time() + g + 15
            self.force_at = time.time() + g
        else:
            self.set_state("stopping", "Stopping")
            self.crosvm_stop(15)

    def run(self):
        signal.signal(signal.SIGTERM, self.on_term)
        signal.signal(signal.SIGINT, self.on_term)
        signal.signal(signal.SIGUSR1, self.on_shutdown)
        signal.signal(signal.SIGUSR2, self.on_force)
        self.agent, self.force_at = None, 0.0
        try:
            self.res = self.prepare()
        except (HelperError, OSError) as e:
            self.release_hugepages()
            self.set_state("failed", str(e))
            return 1
        if config.is_windows(self.cfg):
            self.agent = WinAgent(self)          # clean shutdown over the guest's OpenSSH; no virtio-console
            if self.backend != "qemu" and self.cfg["windows"].get("power_key", False):
                # 0.2-21: a virtio keyboard whose only job is the power key (fallback of Shut Down without ssh);
                # crosvm reads virtio_input_event records from its end of the socket pair
                ours, theirs = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
                self.agent.pk, self._pk_peer = ours, theirs
                self.res["power_key"] = self.fdpath(theirs.fileno())
        else:
            self.agent = Agent(self.rdir, self.log)
            self.res["agent_sock"] = self.agent.path
            if (self.cfg["cloud_init"]["enabled"] and self.cfg["guest_tools"]
                    and self.cfg["network"]["mode"] == "nat"):
                AgentRefresh(self)             # 0.2-24: bring an older guest agent up to date (flags, native Mesa)
        self.inhibit = subprocess.Popen(["systemd-inhibit", "--what=idle", "--mode=block", "--who=Virtual Machines",
                                         "--why=Virtual machine %s is running" % self.cfg["name"], "sleep", "infinity"],
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        con = Console(self.rdir, self.uid, self.gid)
        if self.backend == "qemu":
            argv, env = qemuargs.build(self.cfg, self.res, self.feats)
            binary = [config.QEMU_BIN]
        else:
            argv, env = crosvmargs.build(self.cfg, self.res, self.feats)
            binary = [CROSVM, "--log-level", "info"]
        e = vmm_env(os.environ, env, self.cfg)
        clog = open(os.path.join(self.rdir, "crosvm.log"), "ab", buffering=0)
        self._log_off = os.fstat(clog.fileno()).st_size     # display_watch: only this start's lines (0.2-22)
        os.fchown(clog.fileno(), 0, self.gid)
        os.fchmod(clog.fileno(), 0o640)
        rc, boots = 0, 0
        try:
            while True:
                boots += 1
                self.log("crosvm %s" % " ".join(argv))
                clog.write(("--- boot %d %s\n" % (boots, time.strftime("%Y-%m-%d %H:%M:%S"))).encode())
                con.since = bytearray()
                self.proc = subprocess.Popen(binary + argv, env=e, stdout=clog,
                                             stderr=clog, pass_fds=self.fds, close_fds=True)
                self.state["pid"] = self.proc.pid
                if not con.wait_connected(30):
                    self.log("crosvm did not open the serial port within 30 s")
                self.set_state("running", "")
                last = 0
                while self.proc.poll() is None:
                    time.sleep(0.5)
                    now = time.time()
                    if not self.agent.alive and now - self.last_ping > 2:     # until the first pong: every 2 s
                        self.last_ping = now
                        self.agent.send("ping")
                    if self.shutdown_pending:
                        if self.guest_poweroff():
                            self.shutdown_pending = 0.0
                            self.set_state(self.state["state"], "Shutting down the guest")
                        elif now - self.shutdown_pending > 20:
                            self.shutdown_pending = 0.0
                            self.log("shut down requested but no guest agent answered within 20 s")
                            self.set_state(self.state["state"], "The guest agent does not answer: shut the guest "
                                           "down inside it, or use Force Off")
                    if time.time() - last > 10:
                        last = time.time()
                        self.watchdog()
                        if self.state.get("agent") != self.agent.alive:
                            self.state["agent"] = self.agent.alive
                            self.set_state(self.state["state"])
                    if self.term and self.force_at and time.time() > self.force_at:
                        self.log("the guest did not power off in time: crosvm stop")
                        self.force_at = 0.0
                        self.crosvm_stop(15)
                    if self.term and self.term_at and time.time() > self.term_at:
                        self.log("crosvm did not stop in time: SIGKILL")
                        self.proc.kill()
                        self.term_at = time.time() + 30
                rc = self.proc.returncode
                self.log("crosvm exit %s" % rc)
                time.sleep(0.3)                     # let the relay drain the last console bytes
                # guest reboot: crosvm's code 32, or exit 0 with "reboot: Restarting system" on the console
                # (on Gunyah PSCI SYSTEM_RESET is reported as SYSTEM_OFF; QEMU-Gunyah exits 0 for both)
                # crosvm 0.3-19+ logs "GVM-EXIT type=N" for every Gunyah exit status: 2 / 3 = PSCI SYSTEM_RESET
                # (Windows guests print no console line)
                if not self.term and (rc == 32 or (rc == 0 and (con.guest_rebooted() or self.gvm_exit_reset()))):
                    self.set_state("rebooting", "Guest reboot")
                    self.state["ip"] = None
                    self.stop_forwards()
                    if os.path.lexists(self.res["control_sock"]):
                        os.unlink(self.res["control_sock"])
                    continue
                break
        finally:
            self.stop_forwards()
            con.close()
            self.agent.close()
            self.release_hugepages()
            if self.inhibit:
                self.inhibit.kill()
            msg = "" if rc in (0, None) or self.term else "crosvm exited with code %s (see the log)" % rc
            self.state["pid"] = None
            self.set_state("stopped" if not msg else "failed", msg)
        return 0 if not msg else 1


def verb_supervise(uid, vmid):
    if os.geteuid() != 0 or "INVOCATION_ID" not in os.environ or os.getppid() != 1:
        raise HelperError("supervise runs only inside its systemd unit")
    uid = int(uid)
    try:
        with open("/proc/self/cgroup") as f:
            cg = f.read()
    except OSError:
        cg = ""
    if config.unit_name(uid, vmid) not in cg:
        raise HelperError("supervise runs only inside its own unit")
    return Supervisor(uid, vmid).run()


VMM_MALLOC_ARENAS = 4      # crosvm, 2 GiB venus guest: ~151 MiB anonymous memory by default, ~60 with 4 (or 2) arenas


def vmm_env(base, env, cfg):
    """The VMM's environment: ours + crosvmargs / qemuargs' + the window's title / app id. glibc's default arena count
    (8 per core) left ~90 MiB of mostly free malloc memory in crosvm's 52 threads, so the VMM gets
    MALLOC_ARENA_MAX unless the environment already sets one."""
    e = dict(base, **env)
    e["CROSVM_GVM_WINDOW_TITLE"] = "%s - Virtual Machines" % cfg["name"]
    e["CROSVM_GVM_APP_ID"] = "org.iceland.VirtualMachines"
    e.setdefault("MALLOC_ARENA_MAX", str(VMM_MALLOC_ARENAS))
    return e


VERBS = {"start": (verb_start, 1), "shutdown": (verb_shutdown, 1), "stop": (verb_stop, 1),
         "force-off": (verb_force_off, 1),
         "stop-all": (verb_stop_all, 0), "net-down": (verb_net_down, 0), "unload": (verb_unload, 0),
         "supervise": (verb_supervise, 2)}


def main(args):
    if not args or args[0] not in VERBS or len(args) - 1 != VERBS[args[0]][1]:
        print("usage: iceland-vms-helper start|shutdown|stop ID | stop-all | net-down | unload", file=sys.stderr)
        return 2
    os.umask(0o022)
    limit_malloc_arenas()               # the supervisor lives as long as its VM, with several threads
    try:
        r = VERBS[args[0]][0](*args[1:])
        return r or 0
    except (HelperError, gvmlib.OpError, config.ConfigError) as e:
        print("error: %s" % e, file=sys.stderr)
        return 1
