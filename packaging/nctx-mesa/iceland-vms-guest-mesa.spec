# iceland-vms-guest-mesa - the guest Mesa for the msm native context of iceland-vms (host package; the payload is a
# tarball the app puts into the VM's tools share and the guest agent unpacks under /usr/lib64/iceland-nctx).
# Built by packaging/nctx-mesa/mk-guest-mesa.sh from the build-cross.sh output.
%global debug_package %{nil}
%global __strip /bin/true
Name:           iceland-vms-guest-mesa
Version:        26.2.5
Release:        1.iceland1%{?dist}
Summary:        Guest Mesa (freedreno with the virtio KMD) for the msm native context of iceland-vms VMs
License:        MIT AND BSD-3-Clause AND SGI-B-2.0
ExclusiveArch:  aarch64
Source0:        nctx-mesa-%{version}.tar.gz
Source1:        license.rst
Source2:        README.guest-mesa
Source3:        build-cross.sh

%description
Fedora 44's Mesa %{version} (upstream source, as Fedora ships it) built for aarch64 Fedora guests with freedreno's
virtio kernel-mode driver, which Fedora's own build leaves out: with it the guest's OpenGL (freedreno) and Vulkan
(turnip) run directly on the tablet's Adreno 840 through virglrenderer's msm native context. iceland-vms copies
the tarball into a VM's tools share when the VM's display is "3D: msm native context"; the guest agent unpacks it
under /usr/lib64/iceland-nctx and points Xorg, login shells and user sessions at it. Fedora's Mesa in the guest
is not changed.

%prep

%build

%install
install -D -m 644 %{SOURCE0} %{buildroot}/usr/share/iceland-vms/guest-mesa/nctx-mesa-%{version}.tar.gz
install -D -m 644 %{SOURCE1} %{buildroot}/usr/share/licenses/%{name}/license.rst
install -D -m 644 %{SOURCE2} %{buildroot}/usr/share/doc/%{name}/README.guest-mesa
install -D -m 644 %{SOURCE3} %{buildroot}/usr/share/doc/%{name}/build-cross.sh

%files
%license /usr/share/licenses/%{name}/license.rst
%doc /usr/share/doc/%{name}/README.guest-mesa
%doc /usr/share/doc/%{name}/build-cross.sh
%dir /usr/share/iceland-vms/guest-mesa
/usr/share/iceland-vms/guest-mesa/nctx-mesa-%{version}.tar.gz

%changelog
* Mon Oct 26 2026 mzak <mzak.amin@gmail.com> - 26.2.5-1.iceland1
- first build: Mesa 26.2.5 (Fedora 44's version), gallium freedreno + Vulkan turnip with freedreno-kmds msm,virtio,
  EGL / GLX / GBM for x11 and wayland, libdir /usr/lib64/iceland-nctx (kit-vm22 N2: Fedora's freedreno refused the
  virtio_gpu device, fd_device_new() -> NULL)
