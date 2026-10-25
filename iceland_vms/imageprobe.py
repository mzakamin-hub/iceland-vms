"""Probe a raw disk image on the host before a direct-kernel boot.

Our guest kernel boots without an initramfs, so it cannot resolve root=UUID=... (the V2
failure of 23 Oct 2026: "Cannot open root device UUID=..."). The kernel itself understands
root=PARTUUID=... and /dev/vdXN, so we read the partition table (GPT or MBR), identify the
filesystems by their superblock magic, and for btrfs list the top-level subvolumes to find the
one holding the root filesystem (Fedora: "root", Ubuntu / openSUSE style: "@").

Pure Python, read-only, no root needed (the user owns the image).
"""
import os
import struct
import uuid

SECTOR = 512
LINUX_FS_GUIDS = {
    "0fc63daf-8483-4772-8e79-3d69d8477de4": "linux",
    "b921b045-1df0-41c3-af44-4c6f280d3fae": "root-arm64",
    "4f68bce3-e8cd-4db1-96e7-fbcaf984b709": "root-x86-64",
    "e6d6d379-f507-44c2-a23c-238f2a3df928": "lvm",
    "bc13c2ff-59e6-4262-a352-b275fd6f7172": "xbootldr",
    "c12a7328-f81f-11d2-ba4b-00a0c93ec93b": "esp",
    "21686148-6449-6e6f-744e-656564454649": "bios-boot",
    "0657fd6d-a4ab-43c4-84e5-0933c84b4f4f": "swap",
}


class ProbeError(Exception):
    pass


def _guid(b):
    return str(uuid.UUID(bytes_le=bytes(b)))


class _Img:
    def __init__(self, path):
        self.f = open(path, "rb")
        self.size = os.fstat(self.f.fileno()).st_size

    def read(self, off, n):
        self.f.seek(off)
        d = self.f.read(n)
        return d + b"\0" * (n - len(d))

    def close(self):
        self.f.close()


def image_format(path):
    """raw | qcow2 | iso | zstd | xz | unknown"""
    with open(path, "rb") as f:
        head = f.read(8)
        f.seek(0x8001)
        iso = f.read(5)
    if head[:4] == b"QFI\xfb":
        return "qcow2"
    if head[:4] == b"\x28\xb5\x2f\xfd":
        return "zstd"
    if head[:6] == b"\xfd7zXZ\x00":
        return "xz"
    if iso == b"CD001":
        return "iso"
    return "raw"


def fs_type(img, off):
    """Filesystem at byte offset off: (type, label) or (None, None)."""
    sb = img.read(off + 1024, 1024)
    if struct.unpack_from("<H", sb, 0x38)[0] == 0xEF53:
        lab = sb[0x78:0x88].split(b"\0")[0].decode("utf-8", "replace")
        return "ext4", lab
    x = img.read(off, 512)
    if x[:4] == b"XFSB":
        return "xfs", x[0x6c:0x78].split(b"\0")[0].decode("utf-8", "replace")
    b = img.read(off + 0x10000, 0x1000)
    if b[0x40:0x48] == b"_BHRfS_M":
        return "btrfs", b[0x12b:0x22b].split(b"\0")[0].decode("utf-8", "replace")
    if x[0x36:0x3b] == b"FAT12" or x[0x36:0x3b] == b"FAT16" or x[0x52:0x57] == b"FAT32":
        return "vfat", ""
    s = img.read(off + 4096, 16)
    if s[:10] == b"SWAPSPACE2" or img.read(off + 4086, 10) == b"SWAPSPACE2":
        return "swap", ""
    if img.read(off + 1024, 4) == b"\x10\x20\xf5\xf2":
        return "f2fs", ""
    if img.read(off, 4) in (b"LUKS", b"LUKS\xba"):
        return "luks", ""
    return None, None


def partitions(img):
    """List of dicts: number, start (bytes), size, type, partuuid, name."""
    mbr = img.read(0, SECTOR)
    gpt = img.read(SECTOR, SECTOR)
    out = []
    if gpt[:8] == b"EFI PART":
        lba, count, esize = struct.unpack_from("<QII", gpt, 0x48)
        if not (0 < count <= 256 and 128 <= esize <= 1024):
            raise ProbeError("implausible GPT header")
        table = img.read(lba * SECTOR, count * esize)
        for i in range(count):
            e = table[i * esize:(i + 1) * esize]
            if e[:16] == b"\0" * 16:
                continue
            first, last = struct.unpack_from("<QQ", e, 32)
            t = _guid(e[0:16])
            out.append({
                "number": i + 1,
                "start": first * SECTOR,
                "size": (last - first + 1) * SECTOR,
                "type": LINUX_FS_GUIDS.get(t, t),
                "partuuid": _guid(e[16:32]),
                "name": e[56:128].decode("utf-16-le", "replace").split("\0")[0],
            })
        return out
    if mbr[510:512] == b"\x55\xaa":
        sig = struct.unpack_from("<I", mbr, 0x1b8)[0]
        for i in range(4):
            e = mbr[0x1be + 16 * i:0x1be + 16 * (i + 1)]
            ptype = e[4]
            start, n = struct.unpack_from("<II", e, 8)
            if ptype == 0 or n == 0:
                continue
            if ptype == 0xee:
                raise ProbeError("protective MBR without a readable GPT")
            out.append({"number": i + 1, "start": start * SECTOR, "size": n * SECTOR,
                        "type": "mbr-0x%02x" % ptype, "partuuid": "%08x-%02d" % (sig, i + 1), "name": ""})
    return out


# ---- btrfs: list top-level subvolumes and the default subvolume -------------------------
_HDR = 0x65


class _Btrfs:
    def __init__(self, img, off):
        self.img, self.off = img, off
        sb = img.read(off + 0x10000, 0x1000)
        if sb[0x40:0x48] != b"_BHRfS_M":
            raise ProbeError("no btrfs superblock")
        self.root, self.chunk_root = struct.unpack_from("<QQ", sb, 0x50)
        self.nodesize = struct.unpack_from("<I", sb, 0x94)[0]
        n = struct.unpack_from("<I", sb, 0xa0)[0]
        self.chunks = []          # (logical, length, physical)
        arr = sb[0x32b:0x32b + n]
        p = 0
        while p + 17 + 48 <= len(arr):
            _obj, typ, logical = struct.unpack_from("<QBQ", arr, p)
            p += 17
            length = struct.unpack_from("<Q", arr, p)[0]
            nst = struct.unpack_from("<H", arr, p + 44)[0]
            phys = struct.unpack_from("<Q", arr, p + 48 + 8)[0]
            if typ == 228:
                self.chunks.append((logical, length, phys))
            p += 48 + 32 * nst
        # full chunk tree
        for key, data in self.walk(self.chunk_root):
            if key[1] == 228:
                length = struct.unpack_from("<Q", data, 0)[0]
                phys = struct.unpack_from("<Q", data, 48 + 8)[0]
                if not any(c[0] == key[2] for c in self.chunks):
                    self.chunks.append((key[2], length, phys))

    def phys(self, logical):
        for lo, ln, ph in self.chunks:
            if lo <= logical < lo + ln:
                return self.off + ph + (logical - lo)
        raise ProbeError("btrfs logical address %#x not mapped" % logical)

    def walk(self, logical, depth=0):
        if depth > 8:
            raise ProbeError("btrfs tree too deep")
        node = self.img.read(self.phys(logical), self.nodesize)
        nritems = struct.unpack_from("<I", node, 0x60)[0]
        level = node[0x64]
        if nritems > self.nodesize // 25:
            raise ProbeError("corrupt btrfs node")
        for i in range(nritems):
            if level == 0:
                p = _HDR + 25 * i
                key = struct.unpack_from("<QBQ", node, p)
                doff, dsize = struct.unpack_from("<II", node, p + 17)
                yield key, node[_HDR + doff:_HDR + doff + dsize]
            else:
                p = _HDR + 33 * i
                child = struct.unpack_from("<Q", node, p + 17)[0]
                yield from self.walk(child, depth + 1)

    def subvolumes(self):
        """(names of subvolumes directly below the top level, default subvolume id)."""
        names, default = [], 5
        for key, data in self.walk(self.root):
            if key[1] == 156 and key[0] == 5:            # ROOT_REF from the top-level tree
                ln = struct.unpack_from("<H", data, 16)[0]
                names.append(data[18:18 + ln].decode("utf-8", "replace"))
            elif key[1] == 84 and key[0] == 6:          # DIR_ITEM "default" in ROOT_TREE_DIR
                nl = struct.unpack_from("<H", data, 17 + 8 + 2)[0]     # btrfs_dir_item.name_len
                if data[30:30 + nl] == b"default":
                    default = struct.unpack_from("<Q", data, 0)[0]
        return names, default


def btrfs_root_subvol(img, off):
    """The rootflags subvol= value for this btrfs, or '' when the default subvolume is the root."""
    try:
        names, default = _Btrfs(img, off).subvolumes()
    except (ProbeError, struct.error):
        return None, []
    if default != 5:
        return "", names
    for cand in ("root", "@", "@rootfs", "rootfs"):
        if cand in names:
            return cand, names
    return "", names


def probe(path):
    """Describe an image: format, partitions with filesystems, and the suggested root."""
    fmt = image_format(path)
    res = {"format": fmt, "partitions": [], "root": None, "rootfstype": "", "rootflags": "", "notes": []}
    if fmt != "raw":
        res["notes"].append("not a raw disk image (%s)" % fmt)
        return res
    img = _Img(path)
    try:
        parts = partitions(img)
        if not parts:     # whole-disk filesystem
            t, lab = fs_type(img, 0)
            if t:
                parts = [{"number": 0, "start": 0, "size": img.size, "type": "whole-disk", "partuuid": "", "name": ""}]
        cands = []
        for p in parts:
            p["fs"], p["label"] = fs_type(img, p["start"])
            if p["fs"] == "btrfs":
                p["subvol"], p["subvols"] = btrfs_root_subvol(img, p["start"])
            if p["fs"] in ("ext4", "xfs", "btrfs", "f2fs") and p["type"] not in ("esp", "xbootldr"):
                if p["label"].lower() in ("boot", "/boot") or p["name"].lower() in ("boot",):
                    continue
                cands.append(p)
            res["partitions"].append(p)
        if not cands:
            res["notes"].append("no Linux root filesystem found")
            return res
        cands.sort(key=lambda p: (p["type"] == "root-arm64", p["size"]), reverse=True)
        r = cands[0]
        if r["number"] == 0:
            res["root"] = "/dev/vda"
        elif r["partuuid"]:
            res["root"] = "PARTUUID=" + r["partuuid"]
        else:
            res["root"] = "/dev/vda%d" % r["number"]
        res["rootfstype"] = r["fs"]
        if r["fs"] == "btrfs":
            if r.get("subvol") is None:
                res["notes"].append("btrfs subvolumes could not be read; set rootflags by hand if boot fails")
            elif r["subvol"]:
                res["rootflags"] = "subvol=" + r["subvol"]
        if any(p.get("fs") == "luks" for p in parts):
            res["notes"].append("encrypted (LUKS) partition: direct boot without an initramfs cannot unlock it")
        return res
    finally:
        img.close()


if __name__ == "__main__":
    import json
    import sys
    print(json.dumps(probe(sys.argv[1]), indent=2))
