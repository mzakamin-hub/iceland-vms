# iceland-vms: Virtual Machines for the OnePlus Pad 4

A GTK4 / libadwaita app (Python) that creates, runs and manages **protected virtual machines** on the OnePlus Pad 4
(`iceland`, Qualcomm SM8850 "Kaanapali") running mainline Linux and Fedora 44.

The hypervisor on this SoC is Qualcomm's **Gunyah**. It owns EL2, so there is no KVM: libvirt, QEMU/KVM and GNOME
Boxes cannot work. The VMs run with **crosvm** and its Gunyah backend, as protected VMs (the firmware refuses
unprotected ones). crosvm, the guest kernel and the command-line tool come from the companion package
[iceland-gvm](https://github.com/mzakamin-hub/iceland-gvm); the host kernel's Gunyah modules come with the kernel
from [iceland-linux](https://github.com/mzakamin-hub/iceland-linux) (q90m and later).

App id: `org.iceland.VirtualMachines`. Current version: **0.2-5** (with iceland-gvm 0.3-27).

## What works

- **Linux guests from cloud images** (Fedora 44 Cloud and other distributions' cloud images), booted with the
  tablet's guest kernel; the app finds the root partition itself (GPT / MBR, file-system magic, btrfs subvolume).
- **3D graphics in the guest**: OpenGL through virgl and Vulkan through venus on the tablet's Adreno 840.
  GPU-composited desktops draw in the VM window: Xfce on Xorg with glamor (the default) or the labwc Wayland session.
  The guest kernel syncs the restricted DMA pool for every GPU command buffer, which a protected VM needs for texture
  uploads.
- **Software display** (a framebuffer in shared memory, after DroidVM's design) as the fallback, and a plain 2D
  display.
- **Network**: NAT with DHCP, DNS and HTTPS from the guest; port forwards (firewalld, runtime only).
- **Disks and files**: virtio disks (add, grow, detach), offline snapshots (reflink copies), clone, delete, shared
  folders (idmapped bind mounts, so files the guest creates belong to the user).
- **Console and control**: an interactive serial console, logs, a clean shutdown through a small guest agent on
  `hvc0` (crosvm's power button does nothing for arm64 device-tree guests), force off, and a restart when the guest
  reboots.
- **Guards**: one VM at a time; no start below 20 % battery without a charger and a clean stop at 8 %; a memory
  reserve for the host; a temperature limit; every VM stops cleanly before the tablet suspends.

Not supported yet: Windows guests, installing from an ISO image (UEFI boot reaches the installer's boot menu only),
the QEMU backend (built in, off by default), sound, USB and PCI passthrough.

## How it works

| Part | Runs as | What |
|---|---|---|
| `iceland-vms` (GUI) | the user | VM descriptions in `~/.local/share/gvm/vms/<id>/vm.json`, disks and snapshots next to them, downloads in `~/.local/share/gvm/{isos,images}`. Never root. |
| `iceland-vms-helper` | root, through pkexec (polkit action `org.iceland.vms.manage`) | Narrow verbs: `start`, `shutdown`, `stop`, `force-off ID`, `stop-all`, `net-down`, `unload`. `start` validates the VM, checks the guards, loads the Gunyah modules, brings up the NAT tap and starts one transient systemd unit per VM. |
| supervisor | root, inside that unit | Opens every user file with an `O_NOFOLLOW` walk and an owner check and hands it to crosvm as `/proc/self/fd/N`; relays the serial console to a socket only the user can open; holds the guest agent channel; watches the battery; adds and removes port forwards. |
| guest agent | root in the guest | `gvm-agent` on `hvc0`: ping, power off, reboot, address. Installed by the cloud-init seed. |
| sleep hook | root | `/usr/lib/systemd/system-sleep/iceland-vms`: stops every VM before suspend. |

`data/features.json` (defaults in `/usr/share/iceland-vms`, overrides in `/etc/iceland-vms`) switches each feature on
only when it has been proven on the tablet.

## Install

The app needs Fedora 44 on the OnePlus Pad 4 with kernel-iceland q90m or later (q90r recommended: GPU-composited
guest desktops draw from q90r on) and iceland-gvm 0.3-27 or later.

```bash
sudo dnf install ./iceland-gvm-0.3-27.fc44.aarch64.rpm ./iceland-vms-0.2-5.fc44.noarch.rpm
```

Then start "Virtual Machines" from the application menu.

## Build

```bash
make check                                   # byte-compiles everything and runs the unit tests (no VM is started)
make install DESTDIR=/tmp/root PREFIX=/usr   # what the rpm does; needs the edk2-gunyah build in fw/out (fw/BUILD.md)
```

The rpm: pack the source tree with `fw/out/` as `iceland-vms-0.2.tar.gz` (top directory `iceland-vms-0.2/`) and run
`rpmbuild -bb packaging/iceland-vms.spec` on Fedora 44 (noarch).

## Layout

- `iceland_vms/`: `config` (schema and validation), `crosvmargs` (the crosvm command line), `qemuargs` (the QEMU
  backend, off), `host` (guards and facts), `imageprobe`, `storage`, `seed` (cloud-init), `catalogue`, `download`,
  `gvmlib` (root operations), `helper` (root helper and supervisor), `backend` (GUI side), `ui/` (GTK).
- `data/`: catalogue (made by `tools/gen-catalogue.py`), features, guest tools, desktop / metainfo / polkit files,
  icon, sleep hook.
- `fw/BUILD.md`: how the edk2-gunyah UEFI firmware in the package is built.
- `tests/`: unit tests (`make check`); `tools/vm-iso-test.py`: a headless run of the app's own create path.
- `packaging/iceland-vms.spec`.

## Licence

GPL-2.0-only (`LICENSE`), except the edk2-gunyah firmware that the package carries, which is BSD-2-Clause-Patent
(its licence texts are installed with it; sources in `fw/BUILD.md`).

Related: [iceland-gvm](https://github.com/mzakamin-hub/iceland-gvm) (crosvm, guest kernel, `gvm.sh`),
[iceland-linux](https://github.com/mzakamin-hub/iceland-linux) (the host kernel),
[iceland-mainline](https://github.com/mzakamin-hub/iceland-mainline) (the project).
