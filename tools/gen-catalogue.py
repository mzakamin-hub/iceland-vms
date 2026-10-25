import json, os
R = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
d = json.load(open(R + "/research/distros.json"))
d = d if isinstance(d, list) else d.get("entries", d.get("distros", d))
by = {e["id"]: e for e in d}
out = []
def cloud(did, name, user, notes=""):
    e = by[did]; c = e["cloud"]
    assert c["sha256"] and len(c["sha256"]) == 64 and c["size"]
    ent = {"id": did + "-cloud", "name": name, "version": e["version"], "mode": "cloud-direct",
           "url": c["url"], "size": c["size"], "sha256": c["sha256"], "format": "qcow2",
           "verified_by": ("OpenPGP-signed checksum file, key %s" % c.get("key_fpr")) if c.get("verified") else "checksum file over https (no signature published)",
           "checksum_url": c.get("checksum_url"), "signature_url": c.get("sig_url"),
           "boot": {"root": "PARTUUID=" + c["root_partuuid"], "rootfstype": c["rootfs"],
                    "rootflags": "subvol=root" if c["rootfs"] == "btrfs" else ""},
           "cloud_init": True, "cloud_user": "gvm", "image_default_user": user,
           "own_kernel_restricted_dma": bool(e.get("restricted_dma_pool")),
           "recommended": {"cpus": 4, "memory_mib": 2048, "disk_gib": 20},
           "status": "untested-on-device", "notes": notes}
    out.append(ent)
def iso(did, name, notes):
    e = by[did]; i = e["iso"]
    assert i["sha256"] and len(i["sha256"]) == 64 and i["size"]
    out.append({"id": did + "-iso", "name": name, "version": e["version"], "mode": "iso-uefi",
                "url": i["url"], "size": i["size"], "sha256": i["sha256"], "format": "iso",
                "verified_by": ("OpenPGP-signed SHA256SUMS, key %s" % i.get("key_fpr")) if i.get("verified") else "sha256 file over https (NixOS publishes no signature)",
                "checksum_url": i.get("checksum_url"), "signature_url": i.get("sig_url"),
                "kernel": e.get("kernel_version"), "own_kernel_restricted_dma": True,
                "recommended": {"cpus": 4, "memory_mib": 4096, "disk_gib": 32, "display": "2d"},
                "status": "untested-on-device", "notes": notes})
cloud("fedora-44", "Fedora 44 Cloud", "fedora", "Proven on the tablet (V2, 23 Oct 2026: boot, DHCP, DNS, HTTPS, dnf). Add a desktop with dnf, e.g. @xfce-desktop-environment.")
out[-1]["status"] = "proven"
cloud("ubuntu-26.04", "Ubuntu 26.04 LTS Server (cloud)", "ubuntu", "Its own kernel also supports protected VMs, so it could later boot through UEFI too.")
cloud("debian-13", "Debian 13 (genericcloud)", "debian", "Debian publishes no signature for its cloud images: sha512 checked over https, sha256 pinned here.")
cloud("almalinux-10.2", "AlmaLinux 10.2 (GenericCloud)", "almalinux", "SELinux enforcing; xfs root.")
cloud("rocky-10.2", "Rocky Linux 10.2 (GenericCloud)", "rocky", "SELinux enforcing; xfs root.")
iso("ubuntu-26.04", "Ubuntu 26.04.1 LTS Server installer", "Kernel %s has CONFIG_DMA_RESTRICTED_POOL. Text installer on the serial console or the 2D window; it installs the shim fallback loader, which this firmware needs (no stored boot entries)." % by["ubuntu-26.04"].get("kernel_version"))
iso("ubuntu-26.10", "Ubuntu 26.10 Server installer", "Kernel %s has CONFIG_DMA_RESTRICTED_POOL. Interim release." % by["ubuntu-26.10"].get("kernel_version"))
iso("nixos-26.05", "NixOS 26.05 minimal installer", "Default boot entry (Linux LTS) has CONFIG_DMA_RESTRICTED_POOL. NixOS publishes no signature: the sha256 is from its https checksum file. Install the boot loader with boot.loader.efi.canTouchEfiVariables = false (removable path).")
json.dump({"version": 1, "generated": "2026-10-23", "source": "research/distros.json (kernel configs inspected on the PC)", "entries": out},
          open(R + "/data/catalogue.json", "w"), indent=2)
print(len(out), "entries")
for e in out: print(e["id"], e["size"], e["sha256"][:16], e["verified_by"][:40])
