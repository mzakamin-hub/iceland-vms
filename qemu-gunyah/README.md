# iceland-qemu-gunyah: QEMU with the Gunyah accelerator

The sources of the `iceland-qemu-gunyah` package (release **10.0.2-1**): QEMU 10.0.2 with the Gunyah hypervisor
accelerator, the optional second VM backend of the Virtual Machines app (Settings > General > Backend). crosvm, from
[iceland-gvm](https://github.com/mzakamin-hub/iceland-gvm), stays the default backend.

## What it is

- Upstream QEMU **v10.0.2** (commit `ff3419cb`) plus the patches in `patches/`:
  - `0001`: the Gunyah accelerator (`-accel gunyah`) of
    [qemu-gunyah/qemu-android-gunyah](https://github.com/qemu-gunyah/qemu-android-gunyah) at
    `1eee442b71731ad8bbec9635e579a847f1dead64`, ported onto v10.0.2 without the Android-only changes;
  - `0002` to `0008`: the host's global memory tuning made opt-in, the initrd for Linux guests, fixed 256 MB lend
    chunks, host-visible virtio-gpu blobs / hostmem / venus refused in a protected VM, virtio notifications through the
    kernel's ioeventfd, the shared regions populated and locked before the VM starts, a page-fault report;
  - `0009` to `0011`: virgl fences completed through virglrenderer's sync thread (on by default), and an egl-headless
    switch.
- Protected VMs only: the guest RAM (a memfd, set up by the Virtual Machines app) is lent to the hypervisor, and
  virtio-pci goes through a shared restricted DMA pool.
- OpenGL 3D through virgl (`virtio-gpu-gl-pci`, Fedora 44's virglrenderer) in QEMU's GTK window or over VNC
  (`egl-headless`). Vulkan (venus / zink) and the native context stay with crosvm.
- UEFI firmware `QEMU_EFI.fd` / `QEMU_VARS.fd`: [qemu-gunyah/edk2-gunyah-qemu](https://github.com/qemu-gunyah/edk2-gunyah-qemu)
  at `50c9eb78fa17d012bf850bcb4d06422f850a3126`, `ArmVirtPkg/ArmVirtQemu.dsc`, RELEASE, GCC aarch64.
- Installs only under `/usr/libexec/iceland-qemu-gunyah` and `/usr/share/iceland-qemu-gunyah`, so it never conflicts
  with Fedora's qemu packages.

## Build (Fedora 44, aarch64)

```bash
Q=/path/to/iceland-vms/qemu-gunyah
git clone --branch v10.0.2 https://gitlab.com/qemu-project/qemu.git && cd qemu
git am "$Q"/patches/*.patch
mkdir build && cd build
../configure $(cat "$Q"/configure-line.txt)
ninja qemu-system-aarch64 && DESTDIR=$PWD/stage ninja install
```

`iceland-qemu-gunyah.spec` repackages that staged install. Its `Source0` is a tarball with the top directory
`iceland-qemu-gunyah/` that holds the staged tree (`usr/libexec/iceland-qemu-gunyah/...`, with the binary stripped),
`usr/share/iceland-qemu-gunyah/QEMU_EFI.fd` and `QEMU_VARS.fd` from the edk2 build above, `doc/` (the configure
line, `SOURCES.txt` and these patches in `doc/patches/`) and `licenses/` (the files in `licenses/` here). Then
`rpmbuild -bb iceland-qemu-gunyah.spec`. The package needs Fedora 44's `virglrenderer`.

## Licence

As QEMU itself: the emulator as a whole is GPL-2.0-only, parts are LGPL-2.1-or-later, MIT or BSD (see each source
file); the Gunyah accelerator files are GPL-2.0-or-later. The edk2 firmware is BSD-2-Clause-Patent. The licence texts
are in `licenses/` (`COPYING`, `COPYING.LIB` and `LICENSE` from QEMU, `License-edk2.txt` from edk2) and are installed
with the package.
