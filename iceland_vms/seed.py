"""cloud-init NoCloud seed (vfat, label CIDATA) made as the user with mkfs.vfat + mtools.

Same content as gvm.sh's seed (user, password, ssh keys, our guest modules from the 9p share at
every boot), plus a GVM-READY line on the serial console that the app shows.
"""
import glob
import os
import secrets
import shutil
import string
import subprocess
import tempfile

from . import config, storage


def _tool(name):
    return shutil.which(name) or shutil.which(name, path="/usr/sbin:/sbin") or name


def password(home, cfg):
    p = os.path.join(config.vm_dir(home, cfg["id"]), "guest-password.txt")
    if os.path.exists(p):
        with open(p) as f:
            return f.read().strip()
    pw = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(14))
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(pw + "\n")
    return pw


def _hash(pw):
    r = subprocess.run(["openssl", "passwd", "-6", "-stdin"], input=pw, capture_output=True, text=True, check=True)
    return r.stdout.strip()


def public_keys(home):
    keys = []
    app = storage.ssh_key(home) + ".pub"
    for p in [app] + sorted(glob.glob(os.path.join(home, ".ssh", "id_*.pub"))):
        try:
            with open(p) as f:
                k = f.read().strip()
            if k and k not in keys and "\n" not in k:
                keys.append(k)
        except OSError:
            pass
    return keys


MODULES_CMD = (
    "mkdir -p /mnt/gvmshare; "
    "mount -t 9p -o trans=virtio,version=9p2000.L,ro gvmshare /mnt/gvmshare 2>/dev/null || exit 0; "
    "r=$(uname -r); t=/mnt/gvmshare/modules-$r.tar.gz; "
    "if [ -f \"$t\" ] && [ ! -e /lib/modules/$r/.gvm-$(stat -c %Y \"$t\") ]; then "
    # never let tar replace a /lib -> usr/lib symlink with a directory (that broke every exec in V2, 23 Oct)
    "if [ -L /lib ]; then tar -C /usr -xzf \"$t\"; else tar -C / -xzf \"$t\"; fi && "
    "mkdir -p /lib/modules/$r/extra && "
    # gunyah_guest is built into the guest kernel since 24 Oct: the .ko is only a fallback for a kernel without it
    "if grep -q gunyah_guest /lib/modules/$r/modules.builtin 2>/dev/null; then rm -f /lib/modules/$r/extra/gunyah_guest.ko; "
    "else cp /mnt/gvmshare/extra/*.ko /lib/modules/$r/extra/ 2>/dev/null; fi; depmod -a $r && "
    "touch /lib/modules/$r/.gvm-$(stat -c %Y \"$t\"); "
    "command -v restorecon >/dev/null && restorecon -R /lib/modules/$r; fi; true")

READY_CMD = ("echo \"GVM-READY: $(hostname) kernel $(uname -r) ip "
             "$(ip -4 -o addr show scope global 2>/dev/null | awk '{print $4}' | head -n1)\" > /dev/ttyS0")


def guest_tools_dir():
    inst = os.path.join(config.SHARE_DIR, "guest-tools")
    if os.path.isdir(inst):
        return inst
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "guest-tools")


def _write_file(path, perm, content):
    out = ["  - path: %s" % path, "    permissions: '%s'" % perm, "    content: |"]
    out += ["      " + line for line in content.rstrip("\n").split("\n")]
    return out


def user_data(home, cfg):
    ci = cfg["cloud_init"]
    keys = public_keys(home)
    gt = guest_tools_dir()
    with open(os.path.join(gt, "gvm-agent")) as f:
        agent = f.read()
    with open(os.path.join(gt, "gvm-agent.service")) as f:
        unit = f.read()
    lines = [
        "#cloud-config",
        "hostname: %s" % cfg["id"],
        "users:",
        "  - name: %s" % ci["user"],
        "    groups: [wheel]",          # sudo comes from the sudo: line (Debian / Ubuntu have no wheel: cloud-init creates it)
        "    shell: /bin/bash",
        "    sudo: ALL=(ALL) NOPASSWD:ALL",
        "    lock_passwd: false",
        "    passwd: %s" % _hash(password(home, cfg)),
        "    ssh_authorized_keys:",
    ] + ["      - %s" % k for k in keys] + [
        "ssh_pwauth: false",
        "growpart: {mode: auto, devices: ['/']}",
        "write_files:",
    ] + _write_file("/usr/local/sbin/gvm-agent", "0755", agent) \
      + _write_file("/etc/systemd/system/gvm-agent.service", "0644", unit) + [
        "bootcmd:",
        "  - [sh, -c, %s]" % _yaml_str(MODULES_CMD),
        "  - [sh, -c, 'systemctl mask --now serial-getty@hvc0.service >/dev/null 2>&1; true']",
        "runcmd:",
        "  - [sh, -c, 'systemctl daemon-reload; systemctl enable --now gvm-agent.service']",
        "  - [sh, -c, %s]" % _yaml_str(READY_CMD),
    ]
    return "\n".join(lines) + "\n"


def _yaml_str(s):
    return "'" + s.replace("'", "''") + "'"


def meta_data(cfg):
    # a new instance-id on every seed rewrite makes cloud-init apply it again
    return "instance-id: %s-%s\nlocal-hostname: %s\n" % (cfg["id"], secrets.token_hex(4), cfg["id"])


def write_seed(home, cfg):
    path = os.path.join(config.vm_dir(home, cfg["id"]), cfg["cloud_init"]["seed"])
    with tempfile.TemporaryDirectory(prefix="ivms-seed-") as t:
        ud, md = os.path.join(t, "user-data"), os.path.join(t, "meta-data")
        with open(ud, "w") as f:
            f.write(user_data(home, cfg))
        with open(md, "w") as f:
            f.write(meta_data(cfg))
        tmp = path + ".tmp"
        if os.path.exists(tmp):
            os.unlink(tmp)
        subprocess.run([_tool("mkfs.vfat"), "-n", "CIDATA", "-C", tmp, "4096"], check=True, capture_output=True)
        subprocess.run([_tool("mcopy"), "-i", tmp, ud, md, "::/"], check=True, capture_output=True)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    return path
