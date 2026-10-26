"""crosvm command line for a validated VM description (refactored from iceland-gvm's gvm.sh).

Pure function: the caller (the root helper's supervisor) resolves every file to a path it has
opened safely itself (normally /proc/self/fd/N, which crosvm's open_file_or_duplicate reuses).
The base line is the one proven on the tablet on 23 Oct 2026 (V1 / V1b PASS):
  crosvm run --disable-sandbox --hypervisor gunyah --protected-vm-without-firmware
         --no-usb --no-balloon --cpus N --mem M --swiotlb S -s SOCK ...
"""
from . import config


def cpu_affinity(cfg, sets):
    a = cfg["cpu_affinity"]
    if a == "any":
        return None
    if a == "custom":
        return cfg["cpu_set"]
    cpus = sets.get(a) or sets.get("all") or []
    return ",".join(str(c) for c in cpus) if cpus else None


def vnc_arg(cfg, pwfile):
    """0.2-7: crosvm --vnc-server for display.vnc (127.0.0.1, or every address with vnc_lan + password)."""
    d = cfg["display"]
    if not d.get("vnc"):
        return []
    v = "host=%s,port=%d" % ("0.0.0.0" if d.get("vnc_lan") else "127.0.0.1", d.get("vnc_port", 5900))
    if d.get("vnc_password") and pwfile:
        v += ",password-file=%s" % pwfile
    return ["--vnc-server", v]


def gpu_args(cfg, wayland_sock, feats, vnc_pwfile=None):
    d = cfg["display"]
    mode = d["gpu"]
    if mode == "none":
        return [], {}
    if d.get("vnc"):
        wayland_sock = None              # headless: the VNC server is the display (no session needed)
    disp = "displays=[[mode=windowed[%d,%d]%s]]" % (
        d["width"], d["height"], (",dpi=[%d,%d]" % (d["dpi"], d["dpi"])) if d["dpi"] else "")
    env = {}
    # 0.2-6: the window's touch input mode and fullscreen / monitor (iceland-gvm 0.3-28 crosvm, gvm_input.rs)
    if d.get("mouse", True):
        env["CROSVM_GVM_INPUT_MODE"] = d.get("input_mode", "tablet")
    if d.get("fullscreen"):
        env["CROSVM_GVM_FULLSCREEN"] = "1"
        if d.get("output"):
            env["CROSVM_GVM_OUTPUT"] = d["output"]
    if mode == "simplefb":
        # no virtio-gpu: the guest's simpledrm draws into a framebuffer in host-shared memory that crosvm shows in
        # the window; the window's keyboard and touch reach the guest as virtio-input (crosvm --simplefb)
        args = ["--simplefb", "width=%d,height=%d" % (d["width"], d["height"])]
        if wayland_sock:
            args += ["--wayland-sock", wayland_sock]
        return args + vnc_arg(cfg, vnc_pwfile), env
    if mode == "2d":
        gpu = "backend=2d," + disp
    else:
        # native = the DRM context ONLY: when capset 6 is offered, guest Mesa always takes the native path (V4 A/B 24 Oct)
        # zink (parity step 3): the same device as venus; the guest runs its GL through zink over venus (gvm-agent flag)
        ct = {"virgl": "virgl:virgl2", "native": "drm", "venus": "virgl:virgl2:venus", "zink": "virgl:virgl2:venus"}[mode]
        gpu = ("backend=virglrenderer,context-types=%s,egl=true,gles=true,surfaceless=true,"
               "fixed-blob-mapping=false,pci-bar-size=0x40000000,%s" % (ct, disp))
        env["CROSVM_GVM_ACCEPT"] = "1"           # runtime GuestAccept for blobs (iceland-gvm 0.2, V4)
        # vm-run 24 Oct: the scanout was a UBWC bo (modifier 0x0500000000000001) that no copy path can read
        # (black window); noubwc,notile makes host freedreno allocate it linear
        env["FD_MESA_DEBUG"] = "noubwc,notile"
        env["CROSVM_GVM_SCANOUT_READ"] = "ctx0"    # no CPU dmabuf reads (vm-run v3: all sources agreed)
    args = ["--gpu", gpu]
    if mode in ("venus", "zink"):
        args += ["--gpu-render-server", "path=/usr/libexec/virgl_render_server"]
    if wayland_sock:
        args += ["--wayland-sock", wayland_sock]
    if d["keyboard"]:
        args.append("--display-window-keyboard")
        if d.get("keyboard_grab") and wayland_sock:  # 0.2-22: the window asks the compositor for its shortcuts
            env["CROSVM_GVM_KBD_GRAB"] = "1"
    if d["mouse"] or d.get("vnc"):      # VNC: the pointer device (crosvm adds the absolute tablet)
        args.append("--display-window-mouse")
    return args + vnc_arg(cfg, vnc_pwfile), env


def build(cfg, res, feats=None):
    """argv (without the crosvm binary) and extra environment.

    res (resolved by the supervisor):
      control_sock, serial_sock      paths
      disks                          list of paths, same order as cfg["disks"]
      seed                           path or None (cloud-init NoCloud disk)
      kernel, firmware, initrd       paths (kernel = our guest Image)
      tools_dir                      9p dir with the guest modules, or None
      shares                         list of (dir, tag, kind) already prepared (read-only binds done)
      tap                            tap name or None
      wayland_sock                   path or None
      cpu_sets                       host.cpu_sets()
      power_key                      path of crosvm's end of the power-key socket (Windows) or None
    """
    feats = feats or {}
    shared = cfg.get("memory_mode") == "shared"
    # the V1 / V1b proven prefix (gvm.sh base_args) is kept exactly for protected VMs
    a = ["run", "--disable-sandbox", "--hypervisor", "gunyah",
         "--protected-vm-pseudo-unprotected" if shared else "--protected-vm-without-firmware",
         "--no-usb", "--no-balloon", "--cpus", str(cfg["cpus"]), "--mem", str(cfg["memory_mib"])]
    if not shared:              # shared mode: the host reaches guest RAM directly, no bounce pool
        a += ["--swiotlb", str(cfg["swiotlb_mib"])]
    a += ["-s", res["control_sock"]]
    if cfg.get("hugepages"):
        a.append("--hugepages")
    aff = cpu_affinity(cfg, res.get("cpu_sets", {}))
    if aff:
        a += ["--cpu-affinity", aff]
    a += ["--serial", "type=unix-stream,path=%s,input-unix-stream=true,hardware=serial,num=1,console,earlycon"
          % res["serial_sock"]]
    if res.get("agent_sock"):        # guest agent channel: /dev/hvc0 in the guest (clean shutdown)
        a += ["--serial", "type=unix-stream,path=%s,input-unix-stream=true,hardware=virtio-console,num=1"
              % res["agent_sock"]]
    # 0.2-12: io_uring disks by default (vm-run OPT v10m: the Du variant read 3.3 GB/s, wrote 1.3 GB/s)
    aio = ",async-executor=%s" % cfg.get("disk_async", "uring")
    for d, path in zip(cfg["disks"], res["disks"]):
        a += ["--block", path + (",ro=true" if d["ro"] else "") + aio]
    if res.get("seed"):
        a += ["--block", res["seed"] + ",ro=true" + aio]
    if res.get("tools_dir"):
        a += ["--shared-dir", "%s:gvmshare:type=p9" % res["tools_dir"]]
    for path, tag, kind in res.get("shares", []):
        a += ["--shared-dir", "%s:%s:type=%s" % (path, tag, kind)]
    if cfg["network"]["mode"] == "nat" and res.get("tap"):
        a += ["--net", "tap-name=%s,mac=%s%s" % (res["tap"], cfg["network"]["mac"],
                                                 ",vhost-net" if cfg["network"].get("vhost_net") else "")]
    g, env = gpu_args(cfg, res.get("wayland_sock"), feats, res.get("vnc_password_file"))
    a += g
    if res.get("power_key"):         # 0.2-21: Windows Shut Down fallback (the supervisor holds the other end)
        a += ["--input", "keyboard[path=%s]" % res["power_key"]]
    if cfg["sound"]["mode"] in ("null", "file"):
        a += ["--virtio-snd", "backend=%s" % cfg["sound"]["mode"]]
    if cfg["boot"]["mode"] == "direct":
        if cfg["boot"]["initrd"]:
            a += ["--initrd", res["initrd"]]
        a += ["-p", config.kernel_cmdline(cfg), res["kernel"]]
    else:
        a.append(res["firmware"])          # edk2-gunyah.fd is loaded like a kernel Image (text_offset 0)
        if cfg["boot"].get("firmware") == "ecam":
            env["CROSVM_GVM_PCI_ECAM"] = "1"   # PCI ECAM host bridge for the ACPI firmware (Windows, gvm.sh win)
    return a, env
