# iceland-qemu-gunyah - QEMU 10.0.2 with the Gunyah accelerator for the OnePlus Pad 4 (iceland), the second VM
# backend of the Virtual Machines app (iceland-vms >= 0.2-19). Repackages the staged install made in the project's
# Fedora 44 aarch64 build root (configure line and patches in %{_docdir}/%{name}). Installs only under
# /usr/libexec/iceland-qemu-gunyah and /usr/share/iceland-qemu-gunyah: no conflict with Fedora's qemu packages.

%global _prefix_ice /usr/libexec/iceland-qemu-gunyah
%global debug_package %{nil}
%global __brp_check_rpaths %{nil}

Name:           iceland-qemu-gunyah
Version:        10.0.2
Release:        1%{?dist}
Summary:        QEMU with the Gunyah hypervisor accelerator for the OnePlus Pad 4 (iceland)
# QEMU (GPL-2.0-only, LGPL-2.1-or-later parts, MIT / BSD parts), the Gunyah accelerator (GPL-2.0-or-later),
# edk2 firmware (BSD-2-Clause-Patent)
License:        GPL-2.0-only AND GPL-2.0-or-later AND LGPL-2.1-or-later AND MIT AND BSD-3-Clause AND BSD-2-Clause-Patent
URL:            https://github.com/qemu-gunyah/qemu-android-gunyah
Source0:        qemu-gunyah-%{version}-fc44.aarch64.tar.gz
ExclusiveArch:  aarch64
Requires:       virglrenderer

%description
qemu-system-aarch64 10.0.2 with "-accel gunyah" for protected virtual machines on the OnePlus Pad 4: guest RAM
lent to the Gunyah hypervisor, a shared restricted DMA pool for virtio-pci, OpenGL 3D through virgl
(virtio-gpu-gl, virgl fences completed through virglrenderer's sync thread), the GTK window or a VNC display,
and USB passthrough on qemu-xhci. Used by the Virtual Machines app (iceland-vms) as its QEMU-Gunyah backend;
crosvm (iceland-gvm) stays the default backend.

%prep
%setup -q -c

%build

%install
mkdir -p %{buildroot}%{_prefix_ice} %{buildroot}%{_datadir}/iceland-qemu-gunyah
cp -a iceland-qemu-gunyah/usr/libexec/iceland-qemu-gunyah/. %{buildroot}%{_prefix_ice}/
install -m0644 iceland-qemu-gunyah/usr/share/iceland-qemu-gunyah/QEMU_EFI.fd \
	iceland-qemu-gunyah/usr/share/iceland-qemu-gunyah/QEMU_VARS.fd %{buildroot}%{_datadir}/iceland-qemu-gunyah/
mkdir -p %{buildroot}%{_docdir}/%{name}/patches %{buildroot}%{_licensedir}/%{name}
install -m0644 iceland-qemu-gunyah/doc/configure-line.txt iceland-qemu-gunyah/doc/SOURCES.txt \
	iceland-qemu-gunyah/doc/GL.txt %{buildroot}%{_docdir}/%{name}/
install -m0644 iceland-qemu-gunyah/doc/patches/*.patch %{buildroot}%{_docdir}/%{name}/patches/
install -m0644 iceland-qemu-gunyah/licenses/* %{buildroot}%{_licensedir}/%{name}/

%files
%license %{_licensedir}/%{name}/*
%doc %{_docdir}/%{name}
%{_prefix_ice}
%{_datadir}/iceland-qemu-gunyah

%changelog
* Mon Oct 26 2026 iceland project <noreply@localhost> - 10.0.2-1
- First release: QEMU v10.0.2 + the Gunyah accelerator of qemu-gunyah/qemu-android-gunyah 1eee442b, ported for the
  iceland host kernel. Protected VMs only; guest RAM in a shared memfd (set by the app); OpenGL 3D through virgl with
  the fence sync thread (patch 0011); GTK window or VNC display; USB passthrough. Vulkan (venus) and host-visible
  blobs are refused in a protected VM (they need GuestAccept, which only crosvm has).
- UEFI firmware: qemu-gunyah/edk2-gunyah-qemu 50c9eb78 (ArmVirtQemu RELEASE).
