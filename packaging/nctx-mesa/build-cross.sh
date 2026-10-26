#!/bin/bash
# packaging/nctx-mesa/build-cross.sh - guest Mesa for the msm native context (iceland-vms-guest-mesa), PC side.
# Fedora 44's Mesa 26.2.5 source (the upstream tarball: Fedora's dist-git has no patches for 26.2.5; sha512 checked
# against dist-git's `sources`), cross-compiled for aarch64 against a Fedora 44 aarch64 root (sysroot, R) with
# freedreno's virtio kernel-mode driver ON. Fedora builds freedreno with the default KMD list (msm only), so its
# fd_device_new() has no virtio_device_new() and refuses a virtio_gpu fd: kit-vm22 N2 trace (fd_drm_probe_nctx -> 1,
# fd_device_new -> NULL, no vdrm GET_CAPS / CONTEXT_INIT) -> llvmpipe.
# Only what the native context needs: gallium freedreno, Vulkan turnip, EGL / GLX / GBM, x11 + wayland; no LLVM
# (llvmpipe / zink stay Fedora's). libdir /usr/lib64/iceland-nctx: the guest uses it through the environment only.
# Native helpers: a venv with meson / ninja / mako, Ubuntu's glslang-tools, the F44 root's wayland-scanner (1.26)
# through qemu-user (Ubuntu's 1.22 is too old). The emulated in-chroot build (build-in.sh, ~9 h for Mesa) is replaced.
# Usage: D=work R=f44-aarch64-root DG=mesa-distgit bash build-cross.sh [SRC_TARBALL]  ->  $D/inst (DESTDIR)
set -euo pipefail
D=${D:?set D to the work directory}
R=${R:?set R to a Fedora 44 aarch64 root (sysroot)}
DG=${DG:?set DG to Fedora mesa dist-git (rawhide f44 branch)}
T=${1:-$D/mesa-26.2.5.tar.xz}
mkdir -p $D
if [ ! -d $D/mesa-26.2.5 ]; then
	[ -s "$T" ] || curl -sSfL -o "$T" https://archive.mesa3d.org/mesa-26.2.5.tar.xz
	sha512sum "$T" | awk '{print $1}' | grep -qxF "$(sed -n 's/^SHA512 (mesa-26.2.5.tar.xz) = //p' $DG/sources)" \
		|| { echo "STOP: mesa-26.2.5.tar.xz sha512 differs from Fedora dist-git"; exit 1; }
	tar -xJf "$T" -C $D
fi
[ -x $D/venv/bin/meson ] || { python3 -m venv $D/venv; $D/venv/bin/pip install -q meson ninja mako pyyaml packaging; }
command -v glslangValidator > /dev/null || { echo "need glslang-tools (apt)"; exit 1; }
mkdir -p $D/bin $D/pc
printf '#!/bin/sh\nQEMU_LD_PREFIX=%s exec %s/usr/bin/wayland-scanner "$@"\n' $R $R > $D/bin/wayland-scanner
chmod +x $D/bin/wayland-scanner
WV=$($D/bin/wayland-scanner --version 2>&1 | awk '{print $2}')
printf 'prefix=%s\nbindir=%s/bin\ndatarootdir=/usr/share\npkgdatadir=/usr/share/wayland\nwayland_scanner=%s/bin/wayland-scanner\n\nName: Wayland Scanner\nDescription: Wayland scanner\nVersion: %s\n' \
	$D $D $D "$WV" > $D/pc/wayland-scanner.pc
printf "[binaries]\nwayland-scanner = '%s/bin/wayland-scanner'\n" $D > $D/native.txt
cat > $D/cross-f44.txt << EOT
[binaries]
c = ['aarch64-linux-gnu-gcc', '--sysroot=$R', '-B$R/usr/lib64', '-L$R/usr/lib64']
cpp = ['aarch64-linux-gnu-g++', '--sysroot=$R', '-B$R/usr/lib64', '-L$R/usr/lib64']
ar = 'aarch64-linux-gnu-ar'
strip = 'aarch64-linux-gnu-strip'
pkg-config = 'pkg-config'
[built-in options]
c_link_args = ['-Wl,-rpath-link,$R/usr/lib64', '-Wl,-rpath-link,$R/lib64']
cpp_link_args = ['-Wl,-rpath-link,$R/usr/lib64', '-Wl,-rpath-link,$R/lib64']
[properties]
sys_root = '$R'
pkg_config_libdir = ['$R/usr/lib64/pkgconfig', '$R/usr/share/pkgconfig']
[host_machine]
system = 'linux'
cpu_family = 'aarch64'
cpu = 'armv8-a'
endian = 'little'
EOT
export PATH=$D/bin:$D/venv/bin:$PATH
cd $D/mesa-26.2.5
rm -rf b
meson setup b --cross-file $D/cross-f44.txt --native-file $D/native.txt -Dbuild.pkg_config_path=$D/pc \
	--prefix=/usr --libdir=lib64/iceland-nctx --buildtype=release -Db_ndebug=true \
	-Dplatforms=x11,wayland -Dgallium-drivers=freedreno -Dfreedreno-kmds=msm,virtio \
	-Dvulkan-drivers=freedreno -Dvulkan-layers= \
	-Dgallium-va=disabled -Dteflon=false -Dgallium-rusticl=false \
	-Dgles1=disabled -Dgles2=enabled -Dopengl=true -Dgbm=enabled -Dglx=dri -Degl=enabled -Dglvnd=enabled \
	-Dllvm=disabled -Dvalgrind=disabled -Dbuild-tests=false -Dlibunwind=disabled -Dlmsensors=disabled \
	-Dvideo-codecs= -Dzstd=enabled -Dexpat=enabled > $D/setup.log 2>&1 || { tail -40 $D/setup.log; exit 1; }
grep -E -A1 "freedreno-kmds|gallium-drivers|vulkan-drivers" $D/setup.log | head -8
ninja -C b -j ${JOBS:-12} > $D/ninja.log 2>&1 || { tail -40 $D/ninja.log; exit 1; }
rm -rf $D/inst; DESTDIR=$D/inst ninja -C b install > $D/install.log 2>&1
strings -a $D/inst/usr/lib64/iceland-nctx/libgallium-26.2.5.so | grep -c "No address space" > /dev/null \
	|| { echo "STOP: freedreno virtio backend missing"; exit 1; }
echo BUILD-OK
