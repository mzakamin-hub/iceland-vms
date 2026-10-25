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


def gpu_args(cfg, wayland_sock, feats):
    d = cfg["display"]
    mode = d["gpu"]
    if mode == "none":
        return [], {}
    disp = "displays=[[mode=windowed[%d,%d]%s]]" % (
        d["width"], d["height"], (",dpi=[%d,%d]" % (d["dpi"], d["dpi"])) if d["dpi"] else "")
    env = {}
    if mode == "simplefb":
        # no virtio-gpu: the guest's simpledrm draws into a framebuffer in host-shared memory that crosvm shows in
        # the window; the window's keyboard and touch reach the guest as virtio-input (crosvm --simplefb)
        args = ["--simplefb", "width=%d,height=%d" % (d["width"], d["height"])]
        if wayland_sock:
            args += ["--wayland-sock", wayland_sock]
        return args, env
    if mode == "2d":
        gpu = "backend=2d," + disp
    else:
        # native = the DRM context ONLY: when capset 6 is offered, guest Mesa always takes the native path (V4 A/B 24 Oct)
        ct = {"virgl": "virgl:virgl2", "native": "drm", "venus": "virgl:virgl2:venus"}[mode]
        gpu = ("backend=virglrenderer,context-types=%s,egl=true,gles=true,surfaceless=true,"
               "fixed-blob-mapping=false,pci-bar-size=0x40000000,%s" % (ct, disp))
        env["CROSVM_GVM_ACCEPT"] = "1"           # runtime GuestAccept for blobs (iceland-gvm 0.2, V4)
        # vm-run 24 Oct: the scanout was a UBWC bo (modifier 0x0500000000000001) that no copy path can read
        # (black window); noubwc,notile makes host freedreno allocate it linear
        env["FD_MESA_DEBUG"] = "noubwc,notile"
        env["CROSVM_GVM_SCANOUT_READ"] = "ctx0"    # no CPU dmabuf reads (vm-run v3: all sources agreed)
    args = ["--gpu", gpu]
    if mode == "venus":
        args += ["--gpu-render-server", "path=/usr/libexec/virgl_render_server"]
    if wayland_sock:
        args += ["--wayland-sock", wayland_sock]
    if d["keyboard"]:
        args.append("--display-window-keyboard")
    if d["mouse"]:
        args.append("--display-window-mouse")
    return args, env


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
    """
    feats = feats or {}
    a = ["run", "--disable-sandbox", "--hypervisor", "gunyah", "--protected-vm-without-firmware",
         "--no-usb", "--no-balloon",
         "--cpus", str(cfg["cpus"]), "--mem", str(cfg["memory_mib"]), "--swiotlb", str(cfg["swiotlb_mib"]),
         "-s", res["control_sock"]]
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
    for d, path in zip(cfg["disks"], res["disks"]):
        a += ["--block", path + (",ro=true" if d["ro"] else "")]
    if res.get("seed"):
        a += ["--block", res["seed"] + ",ro=true"]
    if res.get("tools_dir"):
        a += ["--shared-dir", "%s:gvmshare:type=p9" % res["tools_dir"]]
    for path, tag, kind in res.get("shares", []):
        a += ["--shared-dir", "%s:%s:type=%s" % (path, tag, kind)]
    if cfg["network"]["mode"] == "nat" and res.get("tap"):
        a += ["--net", "tap-name=%s,mac=%s" % (res["tap"], cfg["network"]["mac"])]
    g, env = gpu_args(cfg, res.get("wayland_sock"), feats)
    a += g
    if cfg["sound"]["mode"] in ("null", "file"):
        a += ["--virtio-snd", "backend=%s" % cfg["sound"]["mode"]]
    if cfg["boot"]["mode"] == "direct":
        if cfg["boot"]["initrd"]:
            a += ["--initrd", res["initrd"]]
        a += ["-p", config.kernel_cmdline(cfg), res["kernel"]]
    else:
        a.append(res["firmware"])          # edk2-gunyah.fd is loaded like a kernel Image (text_offset 0)
    return a, env
