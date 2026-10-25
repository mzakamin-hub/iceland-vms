# iceland-vms - Virtual Machines app (GTK4 / libadwaita, Python) for protected Gunyah VMs on the OnePlus Pad 4.
# Built from a source tarball of this repository plus fw/out (the edk2-gunyah build, see fw/BUILD.md) on Fedora 44.
Name:           iceland-vms
Version:        0.2
Release:        5%{?dist}
Summary:        Virtual Machines: protected VMs on the Gunyah hypervisor (crosvm)
License:        GPL-2.0-only AND BSD-2-Clause-Patent
BuildArch:      noarch
Source0:        iceland-vms-%{version}.tar.gz
BuildRequires:  make, python3
Requires:       iceland-gvm >= 0.3-27
Requires:       python3-gobject, gtk4, libadwaita
Requires:       polkit, systemd, NetworkManager, firewalld
Requires:       dosfstools, mtools, openssl, openssh-clients, xz, zstd, coreutils
Recommends:     vte291-gtk4, qemu-img

%description
Virtual Machines (org.iceland.VirtualMachines): create, run and manage protected virtual machines with crosvm
on Qualcomm's Gunyah hypervisor (no KVM). Catalogue of checksum-verified guest images and ISOs, ISO install
through the edk2-gunyah UEFI firmware (BSD-2-Clause-Patent, built for upstream crosvm), direct boot of cloud
images with the tablet's guest kernel, disks, shared folders, NAT with port forwards, serial console, offline
snapshots, battery / thermal / suspend guards. The GUI runs as the user; a small root helper (pkexec, polkit
action org.iceland.vms.manage) starts each VM in its own systemd unit.

%prep
%setup -q

%build

%install
make install DESTDIR=%{buildroot} PREFIX=/usr FW=fw/out

%files
%license fw/out/edk2-License.txt fw/out/edk2-gunyah-LICENSE
/usr/bin/iceland-vms
/usr/libexec/iceland-vms
/usr/share/iceland-vms
/usr/share/applications/org.iceland.VirtualMachines.desktop
/usr/share/metainfo/org.iceland.VirtualMachines.metainfo.xml
/usr/share/icons/hicolor/scalable/apps/org.iceland.VirtualMachines.svg
/usr/share/polkit-1/actions/org.iceland.vms.policy
/usr/lib/systemd/system-sleep/iceland-vms
/usr/share/doc/iceland-vms

%changelog
* Sun Oct 25 2026 mzak <mzak.amin@gmail.com> - 0.2-5
- settings: "Accelerated X (glamor)" recommended on; the guest desktop session offers Xorg (glamor, recommended) or
  Wayland (labwc, no longer experimental: vm-run v10h drew it; the session unit of iceland-gvm 0.3-27)

* Sun Oct 25 2026 mzak <mzak.amin@gmail.com> - 0.2-4
- default display: 3D (OpenGL through virgl + Vulkan through venus) with a GPU-composited desktop (Xorg glamor on);
  vm-run v10d: Xorg glamor and weston GL drawn in the window with the iceland-gvm 0.3-23 guest kernel (texture
  uploads synced through the swiotlb). The software display (simplefb) stays as the fallback, 2D for ISO installers
- existing VMs: once, glamor goes back on for a 3D display (it was off as a workaround); the display mode is kept
- ISO / UEFI VMs with a 3D display start with the 2D window (their own distro kernel lacks the swiotlb sync)

* Sat Oct 24 2026 mzak <mzak.amin@gmail.com> - 0.2-3
- helper: a PSCI system reset is recognised from crosvm's "GVM-EXIT type=2|3" line (iceland-gvm 0.3-19+), so a
  guest that prints no console line (Windows) is restarted on reboot instead of being treated as shut down

* Sat Oct 24 2026 mzak <mzak.amin@gmail.com> - 0.2-2
- the software display (shared framebuffer, DroidVM's default) is enabled and the DEFAULT display of new VMs
  (vm-run v8 V8-0..5 PASS: guest simpledrm, host frame drawn, window, KWin); ISO / UEFI VMs keep the 2D window
  (firmware and installers draw through virtio-gpu); 2D and 3D (virgl / venus) stay selectable

* Sat Oct 24 2026 mzak <mzak.amin@gmail.com> - 0.2-1
- display mode "Software display (shared framebuffer, like DroidVM)": crosvm --simplefb, no virtio-gpu, the guest's
  simpledrm draws into host-shared memory (feature "simplefb", enabled after vm-run V8 PASS)
- everything of 0.1-10 (glamor off + Xorg session by default, old-kernel warning)

* Sat Oct 24 2026 mzak <mzak.amin@gmail.com> - 0.1-10
- the desktop that shows in the window (vm-run v5 on q90r): Xorg with glamor OFF (AccelMethod none) is the default
  for every VM, Xorg (not Wayland) the default 3D session; existing VMs are switched once (display.defaults_rev),
  the switches can be turned back on; GL / Vulkan apps in the guest still use virgl / venus
- a warning before starting a 3D VM on a host kernel older than q90r (iceland build 00529)

* Sat Oct 24 2026 mzak <mzak.amin@gmail.com> - 0.1-9
- tests/vm-iso-test.py: no GRUB entry edit by default (v4: the guessed line broke the boot; crosvm's DT
  stdout-path already gives the kernel log on ttyS0), Enter on the default entry; --grub-edit keeps the old way

* Sat Oct 24 2026 mzak <mzak.amin@gmail.com> - 0.1-8
- 3D guests autologin into a Wayland session (labwc) when the guest has one (display.session, default wayland;
  Xorg's scanout stayed black in the window, vm-run V6); 3D scanouts read through ctx0 only

* Sat Oct 24 2026 mzak <mzak.amin@gmail.com> - 0.1-7
- tests/vm-iso-test.py sizes the guest from MemAvailable (2048..4096 MiB; --mem): the host guard refused 4096
- guest agent: zink flag (Xorg / session GL through zink over venus)
- the guest's gunyah_guest.ko is copied only when the guest kernel lacks the built-in (iceland-gvm 0.3-12 has it)

* Sat Oct 24 2026 mzak <mzak.amin@gmail.com> - 0.1-6
- never unload the Gunyah modules: gunyah_rsc_mgr leaves its PM notifier registered, the next suspend oopses
  (q90m / q90n, fixed in q90o); the helper's unload verb logs "kept loaded", the Unload button is hidden
  (/etc/iceland-vms/allow-unload re-enables both on a fixed kernel)
- 3D VMs: FD_MESA_DEBUG=noubwc,notile for crosvm (UBWC scanout = black window)
- create path: a script without a cancel event no longer fails ('NoneType' ... is_set, U1 / U2)

* Sat Oct 24 2026 mzak <mzak.amin@gmail.com> - 0.1-5
- 3D default: OpenGL through virgl + Vulkan through venus (V4 / V4-5 / V4-6 PASS, GuestAccept proven)
- Plasma (KWin): Wayland socket found by name (wayland-N of the user), the crosvm window gets a KWin frame;
  closing the window shuts the guest down cleanly through its agent (no hard VM exit)
- QEMU backend: no hugetlb guest RAM (the host Gunyah driver cannot demand-page hugetlb folios, Q1b)
- older vm.json files: missing keys are filled before validate / save
- create path moved out of the wizard (iceland_vms.create); tests/vm-iso-test.py runs U1 / U2 headless with it

* Sat Oct 24 2026 mzak <mzak.amin@gmail.com> - 0.1-4
- port forwards as a userspace proxy + runtime --add-port in the uplink interface's zone (A7: default zone != Wi-Fi zone,
  forwarding to nm-shared blocked); localhost:<port> works too
- shut down waits up to 20 s for the guest agent (pings every 2 s until the first pong; A5 race)
- display: window title / app id per VM, display-lost watchdog message, autologin + glamor switches
- 4096 MiB proven (M1)

* Sat Oct 24 2026 mzak <mzak.amin@gmail.com> - 0.1-3
- 3D virgl proven (V4 A); native context offers only the DRM context type and is marked experimental

* Sat Oct 24 2026 mzak <mzak.amin@gmail.com> - 0.1-2
- Gunyah host modules from the kernel (modprobe, kernel-iceland q90m+), legacy payload modules only as a fallback
- guest reboot restart, hugetlb for QEMU VMs, THP option, debug firmware, backend auto

* Fri Oct 23 2026 mzak <mzak.amin@gmail.com> - 0.1-1
- first version (vmapp agent): VM list, create wizard (catalogue / ISO / image / test), settings editor,
  serial console, snapshots, logs, host status; root helper + per-VM supervisor; edk2-gunyah firmware
