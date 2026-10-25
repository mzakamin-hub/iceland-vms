# edk2-gunyah firmware build (CAM mode, for upstream crosvm on Gunyah)

The iceland-vms package carries a UEFI firmware for VMs that boot an ISO image or a disk with its own boot loader. It
is DroidVM's edk2 port for Gunyah, built for upstream crosvm (PCI CAM). It is passed to crosvm as the "kernel" of a
protected VM; its VirtioLib bounces all DMA through crosvm's restricted DMA pool.

## Sources

| What | Commit |
|---|---|
| [edk2-gunyah](https://github.com/Droid-VM/edk2-gunyah) (DroidVM) | `90c4d52c7d11485318bbb4fb9291a15ea1550ab2` |
| edk2 submodule (pinned) | `b7a715f7c03c45c6b4575bf88596bfd79658b8ce` (`edk2-stable202602` + later commits); sub-submodules at their pinned commits (`git submodule update --init --recursive`) |
| edk2 patches applied by edk2-gunyah's `scripts/patch.sh` | `patches/edk2/0000..0004` (IORT root complex parser, S5 / ACPI reset, CPU LPI idle, SerialPort CRS) |

Nothing is changed: the firmware is built from these commits as they are.

## Toolchain

Ubuntu 24.04 x86_64, cross compiled: aarch64-linux-gnu-gcc 13.3.0 (`gcc-aarch64-linux-gnu`), iasl 20230628
(`acpica-tools`), `uuid-dev`, GNU Make, Python 3. edk2 toolchain tag `GCC`, arch `AARCH64`,
`GCC_AARCH64_PREFIX=aarch64-linux-gnu-` (set by edk2-gunyah's `build.sh`).

## Commands

```sh
git clone https://github.com/Droid-VM/edk2-gunyah && cd edk2-gunyah
git checkout 90c4d52c7d11485318bbb4fb9291a15ea1550ab2 && git submodule update --init --recursive
EDK2_TARGET=RELEASE bash ./build.sh -D PCI_CAM_MODE=TRUE        # -> edk2-gunyah-cam-release.fd
EDK2_TARGET=DEBUG   bash ./build.sh -D PCI_CAM_MODE=TRUE        # -> edk2-gunyah-cam-debug.fd
EDK2_TARGET=RELEASE bash ./build.sh -D PCI_CAM_MODE=TRUE \
    --pcd gUefiOvmfPkgTokenSpaceGuid.PcdForceNoAcpi=TRUE         # -> edk2-gunyah-cam-release-noacpi.fd (DT only)
```

`PCI_CAM_MODE=TRUE` is the default of `GunyahKernel.dsc`; it is passed so the artifact is unambiguous. CAM mode is
`pci-host-cam-generic`, which upstream crosvm writes.

## Artifacts (`fw/out`, used by `make install`)

| File | Installed as |
|---|---|
| `edk2-gunyah-cam-release-noacpi.fd` | `/usr/share/iceland-vms/firmware/edk2-gunyah.fd` (only the device tree reaches the OS; preferred for Linux guests) |
| `edk2-gunyah-cam-debug.fd` | `/usr/share/iceland-vms/firmware/edk2-gunyah-debug.fd` (debug prints on the 16550 at 0x3f8, crosvm's COM1) |
| `edk2-License.txt`, `edk2-gunyah-LICENSE` | the BSD-2-Clause-Patent texts of edk2 and edk2-gunyah |
| `SHA256SUMS` | the hashes of this build (`fw/SHA256SUMS` in this repository) |

PrePi embeds `__DATE__` / `__TIME__`, so a rebuild is not bit-identical to the hashes in `fw/SHA256SUMS`.
