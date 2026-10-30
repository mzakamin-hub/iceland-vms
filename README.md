# iceland-vms: Virtual Machines for the OnePlus Pad 4

A GTK4 / libadwaita app (Python) that creates, runs and manages **protected virtual machines** on the OnePlus Pad 4
(`iceland`, Qualcomm SM8850 "Kaanapali") running mainline Linux and Fedora 44.

The hypervisor on this SoC is Qualcomm's **Gunyah**. It owns EL2, so there is no KVM: libvirt, QEMU/KVM and GNOME
Boxes cannot work. The VMs run with **crosvm** and its Gunyah backend, as protected VMs (guest RAM lent to the
hypervisor). crosvm, the guest kernel and the command-line tool come from the companion package
[iceland-gvm](https://github.com/mzakamin-hub/iceland-gvm); the host kernel's Gunyah modules come with the kernel
from [iceland-linux](https://github.com/mzakamin-hub/iceland-linux). QEMU with the Gunyah accelerator
(`iceland-qemu-gunyah`, sources in [`qemu-gunyah/`](qemu-gunyah/)) is an optional second backend.

App id: `org.iceland.VirtualMachines`. Current version: **0.2-26** (with iceland-gvm 0.3-59 and iceland-qemu-gunyah 10.0.2-1).

## What works

- **Linux VMs**: protected Gunyah VMs with the tablet's guest kernel; Fedora 44 Cloud images with cloud-init, NAT
  networking with port forwards, virtio disks, shared folders, clean shutdown through the guest agent. The app finds
  the root partition itself (GPT / MBR, file-system magic, btrfs subvolume).
- **3D graphics**: virgl (OpenGL) and venus (Vulkan) on the Adreno GPU, with a GPU-composited desktop in a window on
  the tablet's desktop.
- **msm native context** ("3D: msm native context" in the app; the default for new Fedora 44 and later VMs, other
  guests keep virgl + venus): the guest's own Mesa (freedreno for
  OpenGL, turnip for Vulkan) draws directly on the Adreno 840 through virglrenderer's msm native context; glmark2
  at 1280x800 scores about 2690 (virgl about 1770). The app delivers a matching guest Mesa
  (`iceland-vms-guest-mesa`) into the VM automatically; Fedora's Mesa packages in the guest are not changed.
- **Guest agent**: the guest agent receives the app's display switches (glamor off, autologin, Wayland, zink), and
  the app updates an older agent in a running Linux guest over ssh. Autologin into the desktop with lightdm or GDM
  is on by default, also for existing Linux VMs.
- **QEMU-Gunyah backend (optional)**: Settings > General > Backend; crosvm stays the default. With the
  `iceland-qemu-gunyah` package installed, a VM runs as a protected VM under QEMU 10.0.2 with the Gunyah accelerator,
  with 3D OpenGL (virgl) on the Adreno GPU in QEMU's window or over VNC, and starts and stops cleanly from the app.
  glmark2 at 1280x800 scores about 1390-1410 (crosvm: about 1760). Vulkan (venus / zink) and the native context stay
  crosvm features; the settings say so when QEMU is selected.
- **Software display fallback**: a simple shared framebuffer (no virtio-gpu) for guests without 3D drivers.
- **Touch input modes** on the VM window: tablet (tap = click, long press = right click), touchpad (relative pointer,
  two-finger scroll and tap) and touch screen (multitouch passthrough). They work in portrait and landscape.
- **Fullscreen**: a fullscreen button in the VM's details, optionally on a chosen monitor; the guest display is
  scaled to the output.
- **VNC display**: a VM can serve its display over VNC (local only, or on the network with a password); the details
  page has a button that opens a VNC viewer.
- **Fast networking**: the in-kernel virtio-net path (vhost-net) is on by default; it can be switched off per VM in
  Settings > Network. Measured on the tablet (TCP over the NAT tap, 15 s each way):

  | | guest -> host | host -> guest |
  |---|---|---|
  | crosvm's own network | 2420 MB/s | 1959 MB/s |
  | vhost-net (default) | 2877 MB/s | 4761 MB/s |
- **Performance defaults**: guest memory in 2 MiB pages; io_uring for every disk; the shared I/O bounce buffer is
  limited to 320 MiB (larger pools cannot be set up by the guest kernel).
- **Shared memory mode (opt-in per VM)**: Settings > Memory > Memory protection >
  "Shared with the tablet". The VM still runs under Gunyah, but its RAM is shared with the tablet instead of lent:
  a small boot shim accepts the memory and starts the kernel, so there is no I/O bounce buffer and devices reach
  guest memory directly. **The tablet can read and change all of that VM's memory** (the app shows this warning
  whenever the mode is selected); Protected stays the default for every VM. 3D works in this mode: OpenGL (virgl)
  and Vulkan (venus, and OpenGL through zink on Vulkan), with Vulkan on by default (glmark2 about 1820 at 1280x800);
  the software display and headless VMs work too. It is not uniformly faster - measured on the tablet against the
  same VM in the protected mode:

  | | shared | protected | |
  |---|---|---|---|
  | disk write (256 MiB, fsync) | 260 MB/s | 397 MB/s | -35 % |
  | disk read (cold) | 450 MB/s | 430 MB/s | about the same |
  | network guest -> host | 4494 MB/s | 2611 MB/s | +72 % |
  | network host -> guest | 1484 MB/s | 1940 MB/s | -23 % |
- **Windows 11 (arm64)**: import a prepared image, start it, connect with the Remote Desktop button (KRDC, Remmina
  or FreeRDP to the guest's address) and reach the guest over ssh (the image's OpenSSH server). **Shut Down** in
  the app shuts Windows down cleanly: the first time, the app installs its own ssh key in the guest with your
  default ssh key (the one the image accepts); then it runs `shutdown /s` over ssh and the VM is off in about 7 s.
  If Windows refuses ssh, the app says so at once and offers Force Off.
- **Send shortcuts to the VM** (Settings > Display, off by default): while the VM window has the keyboard focus,
  Super, Alt+Tab, Escape and similar desktop shortcuts reach the guest (crosvm VMs).
- **Console, files and guards**: an interactive serial console and logs; offline snapshots (reflink copies), clone,
  delete; one VM at a time; no start on low battery without a charger or when the tablet is hot; 2 GiB of memory kept for
  the host (a 4 GiB Windows VM starts with apps open); every VM stops before the tablet suspends.

## Requirements and install

kernel-iceland q90u or newer with the Gunyah modules (q90w2 is current; vhost-net needs q90s or newer, older kernels
fall back to crosvm's own network) and iceland-gvm 0.3-59 with its virglrenderer 1.3.0-1.iceland1 (from the
[iceland-gvm 0.3-59 release](https://github.com/mzakamin-hub/iceland-gvm/releases/tag/0.3-59)).
iceland-vms-guest-mesa (from the [0.2-23 release](https://github.com/mzakamin-hub/iceland-vms/releases/tag/0.2-23))
is required since 0.2-24. iceland-qemu-gunyah 10.0.2-1 is needed only for the
QEMU-Gunyah backend.

```bash
sudo dnf install ./iceland-gvm-0.3-59.fc44.aarch64.rpm ./virglrenderer-1.3.0-1.iceland1.fc44.aarch64.rpm \
                 ./iceland-vms-guest-mesa-26.2.5-1.iceland1.fc44.aarch64.rpm ./iceland-vms-0.2-26.fc44.noarch.rpm
sudo dnf install ./iceland-qemu-gunyah-10.0.2-1.fc44.aarch64.rpm    # optional: the QEMU-Gunyah backend
```

Then start "Virtual Machines" from the application menu.

## How it works

| Part | Runs as | What |
|---|---|---|
| `iceland-vms` (GUI) | the user | VM descriptions in `~/.local/share/gvm/vms/<id>/vm.json`, disks and snapshots next to them, downloads in `~/.local/share/gvm/{isos,images}`. Never root. |
| `iceland-vms-helper` | root, through pkexec (polkit action `org.iceland.vms.manage`) | Narrow verbs: `start`, `shutdown`, `stop`, `force-off ID`, `stop-all`, `net-down`, `unload`. `start` validates the VM, checks the guards, loads the Gunyah modules, brings up the NAT tap and starts one transient systemd unit per VM. |
| supervisor | root, inside that unit | Opens every user file with an `O_NOFOLLOW` walk and an owner check and hands it to crosvm (or QEMU) as `/proc/self/fd/N`; relays the serial console to a socket only the user can open; holds the guest agent channel; watches the battery; adds and removes port forwards. |
| guest agent | root in a Linux guest | `gvm-agent` on `hvc0`: ping, power off, reboot, address. Installed by the cloud-init seed. |
| sleep hook | root | `/usr/lib/systemd/system-sleep/iceland-vms`: stops every VM before suspend. |

`data/features.json` (defaults in `/usr/share/iceland-vms`, overrides in `/etc/iceland-vms`) switches each feature on
only when it has been proven on the tablet.

## Build

```bash
make check                                   # byte-compiles everything and runs the unit tests (no VM is started)
make install DESTDIR=/tmp/root PREFIX=/usr   # what the rpm does; needs the edk2-gunyah build in fw/out (fw/BUILD.md)
```

The input tests use iceland-gvm's `gvm/gvm-touchinject.py` and are skipped without it. The rpm: pack the source tree
with `fw/out/` as `iceland-vms-0.2.tar.gz` (top directory `iceland-vms-0.2/`) and run
`rpmbuild -bb packaging/iceland-vms.spec` on Fedora 44 (noarch).

## Layout

- `iceland_vms/`: `config` (schema and validation), `crosvmargs` / `qemuargs` (the crosvm / QEMU command lines),
  `host` (guards and facts),
  `imageprobe`, `storage`, `seed` (cloud-init), `catalogue`, `download`, `create`, `gvmlib` (root operations),
  `helper` (root helper and supervisor), `backend` (GUI side), `ui/` (GTK).
- `data/`: catalogue (made by `tools/gen-catalogue.py`), features, guest tools, desktop / metainfo / polkit files,
  icon, sleep hook.
- `fw/BUILD.md`: how the edk2-gunyah UEFI firmware in the package is built.
- `tests/`: unit tests (`make check`).
- `packaging/iceland-vms.spec`.
- `packaging/nctx-mesa/`: the guest Mesa for the msm native context (`iceland-vms-guest-mesa`): spec, build script,
  Mesa's licence text; see its README.
- `qemu-gunyah/`: the sources of the `iceland-qemu-gunyah` package (patches on QEMU v10.0.2, spec, configure line,
  licence texts); see its README.

## Licence

GPL-2.0-only (`LICENSE`), except the edk2-gunyah firmware that the package carries, which is BSD-2-Clause-Patent
(its licence texts are installed with it; sources in `fw/BUILD.md`). The QEMU patches in `qemu-gunyah/` are under
QEMU's licences (`qemu-gunyah/licenses/`).

Related: [iceland-gvm](https://github.com/mzakamin-hub/iceland-gvm) (crosvm, guest kernel, `gvm.sh`),
[iceland-linux](https://github.com/mzakamin-hub/iceland-linux) (the host kernel),
[iceland-mainline](https://github.com/mzakamin-hub/iceland-mainline) (the project).
