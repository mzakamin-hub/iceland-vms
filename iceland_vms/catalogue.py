"""Built-in catalogue of guest systems (data/catalogue.json).

Only combinations whose real kernel configuration was inspected are listed. Each entry has a
pinned sha256 taken from the distribution's OpenPGP-verified checksum file; downloads are
checked against it before use. "status" says what has been run on the tablet:
  proven              booted on the tablet by the coordinator
  untested-on-device  configs inspected on the PC only
Install modes:
  cloud-direct   cloud image + OUR guest kernel (direct boot, root probed on the host)
  iso-uefi       the distribution's installer ISO through the edk2-gunyah UEFI firmware
                 (needs CONFIG_DMA_RESTRICTED_POOL in the distribution's own kernel)
"""
import json
import os

from . import config

MODES = {
    "cloud-direct": "Ready-made disk image with the tablet's guest kernel (fast, recommended)",
    "iso-uefi": "Installer ISO booted through UEFI firmware (the distribution's own kernel)",
}


def path():
    local = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "catalogue.json")
    inst = os.path.join(config.SHARE_DIR, "catalogue.json")
    return inst if os.path.exists(inst) else local


def load():
    try:
        with open(path(), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    out = []
    for e in data.get("entries", []):
        if not all(k in e for k in ("id", "name", "mode", "url", "sha256", "size")):
            continue
        if e["mode"] not in MODES or not e["url"].startswith("https://") or len(e["sha256"]) != 64:
            continue
        out.append(e)
    return out


def get(eid):
    for e in load():
        if e["id"] == eid:
            return e
    return None


def filename(entry):
    return os.path.basename(entry["url"].split("?")[0])


def store_for(entry):
    return "isos" if entry["mode"] == "iso-uefi" else "images"
