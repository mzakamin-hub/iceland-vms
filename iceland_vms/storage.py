"""VM storage in ~/.local/share/gvm (runs as the user, no root).

  vms/<id>/vm.json            the VM description (config.py)
  vms/<id>/disk0.raw ...      disks (sparse raw; qcow2 possible)
  vms/<id>/seed.img           cloud-init NoCloud seed (vfat, label CIDATA)
  vms/<id>/guest-password.txt (0600) the guest user's password
  vms/<id>/snapshots/<name>/  offline snapshots (reflink copies on btrfs: instant, no extra space)
  isos/   images/             shared ISO / cloud image downloads (checksum-verified)
  ssh/id_ed25519              the app's key for guest access (shutdown, IP query)
"""
import json
import os
import re
import shutil
import subprocess
import time

from . import config, imageprobe

CHUNK = 1 << 20


def ensure_layout(home):
    base = config.data_home(home)
    for d in ("vms", "isos", "images", "ssh"):
        os.makedirs(os.path.join(base, d), mode=0o700, exist_ok=True)
    # no copy-on-write for VM images on btrfs (fragmentation); harmless elsewhere
    for d in ("vms", "isos", "images"):
        subprocess.run(["chattr", "+C", os.path.join(base, d)], capture_output=True)
    return base


def list_vms(home):
    out = []
    d = os.path.join(config.data_home(home), "vms")
    try:
        names = sorted(os.listdir(d))
    except OSError:
        return out
    for n in names:
        p = os.path.join(d, n, "vm.json")
        if config.ID_RE.match(n) and os.path.isfile(p):
            try:
                out.append(config.load(p))
            except (OSError, ValueError):
                continue
    return out


def new_id(home, name):
    base = re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-")[:24] or "vm"
    if not base[0].isalnum():
        base = "vm-" + base
    vid, i = base, 2
    while os.path.exists(config.vm_dir(home, vid)):
        vid = "%s-%d" % (base, i)
        i += 1
    return vid


def save_vm(home, cfg):
    d = config.vm_dir(home, cfg["id"])
    os.makedirs(d, mode=0o700, exist_ok=True)
    config.save(cfg, os.path.join(d, "vm.json"))


def disk_path(home, cfg, disk):
    return os.path.join(config.store_dir(home, disk["store"], cfg["id"]), disk["name"])


# ---- disks --------------------------------------------------------------------------------
def create_disk(path, size_gib, fmt="raw"):
    if os.path.exists(path):
        raise FileExistsError(path)
    if fmt == "qcow2":
        crosvm = os.path.join(config.GVM_DIR, "crosvm")
        r = subprocess.run([crosvm, "create_qcow2", "--size", str(size_gib << 30), path], capture_output=True, text=True)
        if r.returncode:
            raise OSError("crosvm create_qcow2: %s" % r.stderr.strip())
        return
    with open(path, "wb") as f:
        f.truncate(size_gib << 30)


def resize_disk(path, size_gib):
    """Grow a raw disk (offline). Shrinking is refused: it would cut the guest's filesystem."""
    if imageprobe.image_format(path) != "raw":
        raise OSError("only raw disks can be resized offline")
    cur = os.path.getsize(path)
    new = size_gib << 30
    if new < cur:
        raise OSError("shrinking a disk is not supported (current %.1f GiB)" % (cur / (1 << 30)))
    os.truncate(path, new)


def sparse_write(src, dst_path, progress=None, total=None):
    """Copy a stream into dst_path, leaving holes for all-zero 1 MiB blocks."""
    done = 0
    with open(dst_path, "wb") as out:
        while True:
            b = src.read(CHUNK)
            if not b:
                break
            if b.count(0) == len(b):
                out.seek(len(b), os.SEEK_CUR)
            else:
                out.write(b)
            done += len(b)
            if progress:
                progress(done, total)
        out.truncate(done)
    return done


def import_image(src, dst, progress=None):
    """Make a raw disk dst from a downloaded image (raw, .xz, .zst, qcow2), sparse."""
    fmt = imageprobe.image_format(src)
    tmp = dst + ".part"
    if fmt in ("xz", "zstd"):
        tool = ["xz", "-dc", src] if fmt == "xz" else ["zstd", "-dcq", src]
        p = subprocess.Popen(tool, stdout=subprocess.PIPE)
        sparse_write(p.stdout, tmp, progress)
        if p.wait():
            os.unlink(tmp)
            raise OSError("%s failed" % tool[0])
        if imageprobe.image_format(tmp) == "qcow2":
            os.replace(tmp, tmp + ".q")
            _qcow2_to_raw(tmp + ".q", tmp)
            os.unlink(tmp + ".q")
    elif fmt == "qcow2":
        _qcow2_to_raw(src, tmp)
    else:
        r = subprocess.run(["cp", "--reflink=auto", "--sparse=always", src, tmp], capture_output=True, text=True)
        if r.returncode:
            raise OSError("copy failed: %s" % r.stderr.strip())
    os.replace(tmp, dst)


def _qcow2_to_raw(src, dst):
    if not shutil.which("qemu-img"):
        raise OSError("qemu-img is needed to convert qcow2 images (dnf install qemu-img)")
    r = subprocess.run(["qemu-img", "convert", "-O", "raw", src, dst], capture_output=True, text=True)
    if r.returncode:
        raise OSError("qemu-img convert: %s" % r.stderr.strip())


# ---- clone / snapshots / delete ---------------------------------------------------------
def _reflink(src, dst):
    r = subprocess.run(["cp", "--reflink=auto", "--sparse=always", "-p", src, dst], capture_output=True, text=True)
    if r.returncode:
        raise OSError("copy %s: %s" % (os.path.basename(src), r.stderr.strip()))


def clone_vm(home, cfg, new_name):
    nid = new_id(home, new_name)
    src, dst = config.vm_dir(home, cfg["id"]), config.vm_dir(home, nid)
    os.makedirs(dst, mode=0o700)
    try:
        for d in cfg["disks"]:
            if d["store"] == "vm":
                _reflink(os.path.join(src, d["name"]), os.path.join(dst, d["name"]))
        new = json.loads(json.dumps(cfg))
        new["id"], new["name"] = nid, new_name
        new["network"]["mac"] = config.random_mac()
        new["autostart"] = False
        save_vm(home, new)
        if new["cloud_init"]["enabled"]:
            from . import seed
            seed.write_seed(home, new)
        return new
    except Exception:
        shutil.rmtree(dst, ignore_errors=True)
        raise


def snapshots(home, cfg):
    d = os.path.join(config.vm_dir(home, cfg["id"]), "snapshots")
    out = []
    for n in sorted(os.listdir(d)) if os.path.isdir(d) else []:
        meta = os.path.join(d, n, "snapshot.json")
        try:
            with open(meta) as f:
                m = json.load(f)
            m["dir"] = n
            out.append(m)
        except (OSError, ValueError):
            continue
    return out


def take_snapshot(home, cfg, label):
    vd = config.vm_dir(home, cfg["id"])
    name = time.strftime("%Y%m%d-%H%M%S")
    sd = os.path.join(vd, "snapshots", name)
    os.makedirs(sd, mode=0o700)
    try:
        files = []
        for d in cfg["disks"]:
            if d["store"] == "vm" and not d["ro"]:
                _reflink(os.path.join(vd, d["name"]), os.path.join(sd, d["name"]))
                files.append(d["name"])
        shutil.copy2(os.path.join(vd, "vm.json"), os.path.join(sd, "vm.json"))
        with open(os.path.join(sd, "snapshot.json"), "w") as f:
            json.dump({"label": label[:64], "time": int(time.time()), "files": files}, f)
    except Exception:
        shutil.rmtree(sd, ignore_errors=True)
        raise
    return name


def restore_snapshot(home, cfg, name):
    vd = config.vm_dir(home, cfg["id"])
    sd = os.path.join(vd, "snapshots", name)
    with open(os.path.join(sd, "snapshot.json")) as f:
        meta = json.load(f)
    for fn in meta["files"]:
        if not config.FILE_RE.match(fn):
            continue
        _reflink(os.path.join(sd, fn), os.path.join(vd, fn + ".restore"))
        os.replace(os.path.join(vd, fn + ".restore"), os.path.join(vd, fn))
    shutil.copy2(os.path.join(sd, "vm.json"), os.path.join(vd, "vm.json"))


def delete_snapshot(home, cfg, name):
    if not re.match(r"^\d{8}-\d{6}$", name):
        raise ValueError("bad snapshot name")
    shutil.rmtree(os.path.join(config.vm_dir(home, cfg["id"]), "snapshots", name))


def delete_vm(home, cfg):
    shutil.rmtree(config.vm_dir(home, cfg["id"]))


def usage(home, cfg):
    """Allocated bytes of the VM folder (sparse-aware)."""
    total = 0
    for root, _dirs, files in os.walk(config.vm_dir(home, cfg["id"])):
        for f in files:
            try:
                total += os.lstat(os.path.join(root, f)).st_blocks * 512
            except OSError:
                pass
    return total


def sync_autostart(home):
    """~/.config/autostart entry exists exactly when some VM has autostart on."""
    dst = os.path.join(home, ".config", "autostart", "org.iceland.VirtualMachines.desktop")
    want = any(v.get("autostart") for v in list_vms(home))
    if want and not os.path.exists(dst):
        src = os.path.join(config.SHARE_DIR, "org.iceland.VirtualMachines.autostart.desktop")
        if not os.path.exists(src):
            src = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data",
                               "org.iceland.VirtualMachines.autostart.desktop")
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copyfile(src, dst)
    elif not want and os.path.exists(dst):
        os.unlink(dst)


# ---- the app's ssh key for guest access ---------------------------------------------------
def ssh_key(home):
    k = os.path.join(config.data_home(home), "ssh", "id_ed25519")
    if not os.path.exists(k):
        os.makedirs(os.path.dirname(k), mode=0o700, exist_ok=True)
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "iceland-vms", "-f", k], check=True)
    return k
