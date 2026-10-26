"""QEMU-Gunyah (qemu-gunyah/qemu-android-gunyah 10.0.2, ported to Fedora: iceland-qemu-gunyah) command
line for a validated VM description. 0.2-19: enabled (features qemu_backend + qemu_gl) after qemu-3d-run QG5-QG7
on q90u; crosvm stays the default backend. The machine line is the one the QG / Q1 rows ran (gic-version=3, guest
RAM in a shared memfd, virtio-gpu-gl with blob off, the virgl fence sync thread on by default in QEMU patch 0011).

Same protected-VM model as crosvm (from the QEMU-Gunyah evaluation): guest RAM lent with
GH_VM_ANDROID_LEND_USER_MEM, the top swiotlb-size of -m shared and declared as restricted-dma-pool,
virtio-pci forced to ACCESS_PLATFORM, no MSI (INTx doorbells), PL011 console (ttyAMA0). It therefore
also needs OUR guest kernel (CONFIG_DMA_RESTRICTED_POOL) - a guest without it would make the host
kernel touch lent pages (panic risk), so the app never offers stock-kernel ISOs on this backend.
What it adds over crosvm: USB passthrough (qemu-xhci + usb-host), QEMU's GTK window.
"""
from . import config

QEMU_FIRMWARE = "/usr/share/iceland-qemu-gunyah/QEMU_EFI.fd"     # qemu-gunyah/edk2-gunyah-qemu 50c9eb78


def kernel_cmdline(cfg):
    # same as crosvm, but the console is the PL011
    return config.kernel_cmdline(cfg).replace("console=ttyS0", "console=ttyAMA0")


def pci_devices(argv):
    """Number of PCI devices in an argv (the fork shares 4 INTx lines among them)."""
    return sum(1 for i, x in enumerate(argv) if x == "-device" and argv[i + 1].split(",")[0].endswith("-pci")) +         sum(1 for x in argv if x == "-virtfs") + sum(1 for i, x in enumerate(argv) if x == "-device" and argv[i + 1].startswith("qemu-xhci"))


def hugepages_needed(cfg):
    """2 MiB hugetlb pages to reserve for this VM: always 0.  The host Gunyah driver's
    gunyah_demand_page() rejects hugetlb folios (not swap-backed -> -EIO; tablet row Q1b),
    so QEMU guest RAM stays on the default backend, which the fork backs with THP."""
    return 0


def build(cfg, res, feats=None):
    """argv (without the binary) and extra environment; res as in crosvmargs.build."""
    mem_total = cfg["memory_mib"] + cfg["swiotlb_mib"]      # QEMU's -m includes the shared swiotlb
    a = ["-nodefaults", "-no-user-config",
         "-accel", "gunyah,protected=on",
         "-object", "arm-confidential-guest,id=cg0,swiotlb-size=%dM" % cfg["swiotlb_mib"],
         # 0.2-9: guest RAM in a shared memfd like crosvm's: with anonymous private RAM the shared swiotlb went
         # through the host's demand-share path and failed -22 (qemu-3d-run p12 / p3 runs A and B)
         "-object", "memory-backend-memfd,id=gram,size=%dM,share=on" % mem_total,
         "-M", "virt,gic-version=3,confidential-guest-support=cg0,memory-backend=gram",
         "-cpu", "host", "-smp", str(cfg["cpus"]), "-m", "%dM" % mem_total,
         "-qmp", "unix:%s,server=on,wait=off" % res["control_sock"],
         "-chardev", "socket,id=ser0,path=%s" % res["serial_sock"],          # the supervisor listens
         "-serial", "chardev:ser0"]
    # cfg["hugepages"]: no hugetlb memfd here (Q1b, see hugepages_needed); the fork's own
    # LEND preparation already applies MADV_HUGEPAGE + MADV_COLLAPSE to the default RAM.
    if res.get("agent_sock"):
        a += ["-device", "virtio-serial-pci,id=vser0",
              "-chardev", "socket,id=agent0,path=%s" % res["agent_sock"],
              "-device", "virtconsole,chardev=agent0"]
    for i, (d, path) in enumerate(zip(cfg["disks"], res["disks"])):
        a += ["-drive", "file=%s,format=raw,if=none,id=d%d%s" % (path, i, ",readonly=on" if d["ro"] else ""),
              "-device", "virtio-blk-pci,drive=d%d" % i]
    if res.get("seed"):
        a += ["-drive", "file=%s,format=raw,if=none,id=seed,readonly=on" % res["seed"],
              "-device", "virtio-blk-pci,drive=seed"]
    if res.get("tools_dir"):
        a += ["-virtfs", "local,path=%s,mount_tag=gvmshare,security_model=none,readonly=on" % res["tools_dir"]]
    for path, tag, kind in res.get("shares", []):
        a += ["-virtfs", "local,path=%s,mount_tag=%s,security_model=passthrough" % (path, tag)]
    if cfg["network"]["mode"] == "nat" and res.get("tap"):
        a += ["-netdev", "tap,id=n0,ifname=%s,script=no,downscript=no,vhost=off" % res["tap"],
              "-device", "virtio-net-pci,netdev=n0,mac=%s,romfile=" % cfg["network"]["mac"]]
    env = {}
    if cfg["display"]["gpu"] != "none":
        d = cfg["display"]
        # 0.2-7 (feature qemu_gl, qemu-3d-run v2 QG rows): virgl GL on QEMU = virtio-gpu-gl-pci with blob off (blob /
        # hostmem / venus need GuestAccept, refused by patch 0005 in a protected VM); needs the dmasync guest kernel
        # 0.2-19: virgl only (venus / zink / native are refused for QEMU by config.backend_conflicts)
        gl = bool((feats or {}).get("qemu_gl")) and d["gpu"] == "virgl"
        a += ["-device", "%s,xres=%d,yres=%d%s" % ("virtio-gpu-gl-pci" if gl else "virtio-gpu-pci", d["width"],
                                                   d["height"], ",blob=false" if gl else "")]
        if d.get("vnc"):                 # 0.2-7: QEMU's own VNC server, this tablet only (127.0.0.1)
            a += ["-display", "egl-headless" if gl else "none",
                  "-vnc", "127.0.0.1:%d" % (d.get("vnc_port", 5900) - 5900)]
        else:
            a += ["-display", "gtk,gl=on,zoom-to-fit=on,show-menubar=off" if gl else "gtk,gl=off,zoom-to-fit=on"]
        # input on the xHCI controller: one PCI device instead of two (no MSI under Gunyah: all PCI
        # devices share 4 INTx lines and the fork's doorbell only fires on a 0->1 edge)
        if cfg["display"]["keyboard"] or cfg["display"]["mouse"] or cfg.get("usb"):
            a += ["-device", "qemu-xhci,id=xhci0"]
        if cfg["display"]["keyboard"]:
            a += ["-device", "usb-kbd,bus=xhci0.0"]
        if cfg["display"]["mouse"]:
            a += ["-device", "usb-tablet,bus=xhci0.0"]
        if res.get("wayland_sock"):
            env.update(WAYLAND_DISPLAY=res["wayland_sock"], GDK_BACKEND="wayland",
                       XDG_RUNTIME_DIR=res["wayland_sock"].rsplit("/", 1)[0])
    else:
        a += ["-display", "none"]
    if cfg.get("usb"):
        if "qemu-xhci,id=xhci0" not in a:
            a += ["-device", "qemu-xhci,id=xhci0"]
        for u in cfg["usb"]:
            a += ["-device", "usb-host,bus=xhci0.0,vendorid=0x%s,productid=0x%s" % (u["vendor"], u["product"])]
    if cfg["boot"]["mode"] == "direct":
        if cfg["boot"]["initrd"]:
            a += ["-initrd", res["initrd"]]
        a += ["-kernel", res["kernel"], "-append", kernel_cmdline(cfg)]
    else:
        a += ["-bios", res.get("qemu_firmware") or QEMU_FIRMWARE]
    return a, env
