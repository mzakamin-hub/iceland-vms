"""Create a VM from a source (catalogue entry / ISO / disk image / test shell) - no GTK.

The wizard runs this in a thread; tools/vm-create.py and the tablet test scripts call it directly,
so a scripted test exercises exactly the app's own create path.

src = dict(kind="catalogue", entry=<catalogue entry>) | dict(kind="iso", path=...) |
      dict(kind="image", path=...) | dict(kind="test") |
      dict(kind="windows", path=<a set-up Windows 11 ARM64 raw disk>)   (0.2-12)
v   = dict(name=..., cpus=..., mem=<MiB>, disk=<GiB>, net=<bool>, gpu="2d"|"none"|...)
"""
import os
import shutil

from . import catalogue, config, download, imageprobe, seed, storage


def _noop(*_a, **_k):
    pass


def fetch(home, entry, dl_progress=None, cancel=None):
    """Download a catalogue entry into its store and verify its sha256 (kept if already verified)."""
    store = catalogue.store_for(entry)
    dest = os.path.join(config.store_dir(home, store), catalogue.filename(entry))
    d = download.Download(entry["url"], dest, entry["sha256"], entry["size"], progress=dl_progress)
    if cancel is not None:              # scripts pass none: keep Download's own event (U1 / U2 24 Oct)
        d.cancel = cancel
    d._fetch()
    return dest


def add_iso(home, path):
    name = os.path.basename(path)
    if not config.FILE_RE.match(name):
        name = "".join(c if c.isalnum() or c in "._-" else "_" for c in name).lstrip("._-")[:120] or "image.iso"
    dst = os.path.join(config.store_dir(home, "isos"), name)
    if not os.path.exists(dst):
        try:
            os.link(path, dst)
        except OSError:
            storage._reflink(path, dst)
    return dst


def create_vm(home, src, v, progress=None, dl_progress=None, cancel=None):
    """Returns the saved cfg; removes the half-made VM directory on any error."""
    progress = progress or _noop
    storage.ensure_layout(home)
    vid = storage.new_id(home, v["name"])
    cfg = config.default_config(vid, v["name"])
    cfg.update(cpus=v["cpus"], memory_mib=v["mem"])
    cfg["network"]["mode"] = "nat" if v["net"] else "off"
    cfg["display"]["gpu"] = v["gpu"]
    vd = config.vm_dir(home, vid)
    os.makedirs(vd, mode=0o700)
    try:
        kind = src["kind"]
        if kind == "windows":
            # 0.2-12: import a prepared Windows 11 ARM64 disk (route A: DroidVM
            # drivers, test signing, OpenSSH). The source file is never changed: the VM gets its own copy (a
            # reflink on btrfs: instant and no extra space until the guest writes); the app never resets it.
            config.windows_defaults(cfg)
            cfg.update(cpus=v["cpus"], memory_mib=v["mem"])
            cfg["network"]["mode"] = "nat" if v["net"] else "off"
            if v.get("ssh_user"):
                cfg["windows"]["ssh_user"] = v["ssh_user"]
            if imageprobe.image_format(src["path"]) != "raw":
                raise RuntimeError("a Windows image must be a raw disk (.raw / .img)")
            progress("Copying the Windows disk", None, "%s (a reflink on btrfs)" % os.path.basename(src["path"]))
            storage.import_image(src["path"], os.path.join(vd, "disk0.raw"))
            cfg["disks"] = [{"store": "vm", "name": "disk0.raw", "kind": "disk", "ro": False}]
            storage.save_vm(home, cfg)
            return cfg
        if kind == "test":
            cfg["boot"]["initrd"] = True
            cfg["guest_tools"] = False
        elif kind == "iso" or (kind == "catalogue" and src["entry"]["mode"] == "iso-uefi"):
            if kind == "catalogue":
                iso = fetch(home, src["entry"], dl_progress, cancel)
                cfg["os"] = src["entry"]["id"]
            else:
                progress("Adding the ISO", None, os.path.basename(src["path"]))
                iso = add_iso(home, src["path"])
            progress("Creating the disk", None)
            storage.create_disk(os.path.join(vd, "disk0.raw"), v["disk"])
            cfg["boot"]["mode"] = "uefi"
            if v["gpu"] in ("simplefb", "venus", "virgl", "native", "zink"):
                # the firmware and the ISO installers draw through virtio-gpu (EFI GOP); the software display is
                # for our direct-boot guests until a UEFI run proves it, and 3D needs our guest kernel (the
                # swiotlb sync of 0.3-23): an installed distro kernel would show a black GL desktop
                cfg["display"]["gpu"] = "2d"
            cfg["swiotlb_mib"] = 256 if v["gpu"] != "none" else 128
            cfg["disks"] = [{"store": "isos", "name": os.path.basename(iso), "kind": "cdrom", "ro": True},
                            {"store": "vm", "name": "disk0.raw", "kind": "disk", "ro": False}]
        else:
            if kind == "catalogue":
                img = fetch(home, src["entry"], dl_progress, cancel)
                cfg["os"] = src["entry"]["id"]
            else:
                img = src["path"]
            progress("Unpacking the disk image", None, os.path.basename(img))
            disk = os.path.join(vd, "disk0.raw")
            storage.import_image(img, disk, progress=lambda d, t: progress(
                "Unpacking the disk image", None, "%d MiB" % (d >> 20)))
            if os.path.getsize(disk) < v["disk"] << 30:
                storage.resize_disk(disk, v["disk"])
            cfg["disks"] = [{"store": "vm", "name": "disk0.raw", "kind": "disk", "ro": False}]
            progress("Looking for the root filesystem", None)
            pr = imageprobe.probe(disk)
            ent = src.get("entry") or {}
            b = ent.get("boot", {})
            cfg["boot"]["root"] = pr["root"] or b.get("root", "")
            cfg["boot"]["rootfstype"] = pr["rootfstype"] or b.get("rootfstype", "")
            cfg["boot"]["rootflags"] = pr["rootflags"] if pr["root"] else b.get("rootflags", "")
            if not cfg["boot"]["root"]:
                raise RuntimeError("No Linux root filesystem found in the image (%s)" % "; ".join(pr["notes"]))
            cfg["cloud_init"]["enabled"] = ent.get("cloud_init", True)
            if ent.get("cloud_user"):
                cfg["cloud_init"]["user"] = ent["cloud_user"]
        storage.save_vm(home, cfg)
        if cfg["cloud_init"]["enabled"]:
            progress("Writing the cloud-init seed", None)
            seed.write_seed(home, cfg)
        return cfg
    except BaseException:
        shutil.rmtree(vd, ignore_errors=True)
        raise
