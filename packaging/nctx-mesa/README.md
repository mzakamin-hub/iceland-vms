# iceland-vms-guest-mesa: the guest Mesa for the msm native context

`iceland-vms-guest-mesa-26.2.5-1.iceland1` carries a Mesa build for Fedora 44 aarch64 **guests**. With it a VM whose
display is "3D: msm native context" draws with freedreno (OpenGL) and turnip (Vulkan) directly on the tablet's
Adreno 840, through virglrenderer's msm native context. Fedora's own Mesa builds freedreno with the msm kernel-mode
driver only, so inside a VM (a virtio_gpu device) it falls back to software rendering.

## Source

- Mesa **26.2.5**, the upstream release tarball `mesa-26.2.5.tar.xz` from <https://archive.mesa3d.org/>, the same
  version and tarball as Fedora 44's `mesa` package. Fedora's dist-git (`mesa`, commit `4d53ade` "Update to 26.2.5
  upstream release") lists it in `sources` as
  `SHA512 (mesa-26.2.5.tar.xz) = 08c7d7ea426ddfb0f7bc51b06e18c363a4d31cf1a8b203828bb455eda0189e41d2215eb9f627585ed11e0d0f231a6a8c9f0c7176238760cd76b62e20c35ccd6a`;
  `build-cross.sh` checks this sum. Fedora applies **no patches** to 26.2.5, and none are added here.
- Build options (all in `build-cross.sh`): `-Dgallium-drivers=freedreno -Dvulkan-drivers=freedreno
  -Dfreedreno-kmds=msm,virtio -Dplatforms=x11,wayland -Dllvm=disabled --libdir=lib64/iceland-nctx`, release build;
  EGL, GLX (glvnd) and GBM on; no VA-API, OpenCL or LLVM.

## Build

1. `build-cross.sh` cross-compiles Mesa for aarch64 against a Fedora 44 aarch64 root (sysroot) and installs it into
   `$D/inst`: `D=<work dir> R=<Fedora 44 aarch64 root> DG=<Fedora mesa dist-git> bash build-cross.sh`.
2. The package's payload `nctx-mesa-26.2.5.tar.gz` is `/usr/lib64/iceland-nctx/` from that install, runtime files
   only and stripped: `libgallium-26.2.5.so`, `libEGL_mesa.so.0*`, `libGLX_mesa.so.0*`, `libgbm.so.1*`,
   `libvulkan_freedreno.so`, `gbm/dri_gbm.so`, `dri/*`, plus `freedreno_icd.json` (Mesa's ICD file with the library
   path `/usr/lib64/iceland-nctx/libvulkan_freedreno.so`), `env.sh` and `license.rst`; packed with
   `tar --sort=name --owner=0 --group=0 --numeric-owner | gzip -9n`.
3. `rpmbuild -bb iceland-vms-guest-mesa.spec` with the tarball, `license.rst`, `README.guest-mesa` and
   `build-cross.sh` in `SOURCES`.

How the app uses it: `README.guest-mesa`.

## Licence

Mesa's licences (mostly MIT; see `license.rst`, Mesa's own licence document).
