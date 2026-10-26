// SPDX-License-Identifier: GPL-2.0-only
// gvm-fbread.c - (in the guest, as root) read back what the CRTC currently scans out, from the HOST side of the
// resource: DRM_IOCTL_MODE_GETFB2 -> GEM handle -> VIRTGPU_TRANSFER_FROM_HOST into the guest backing -> mmap ->
// statistics. iceland gvm, vm-run v10c: separates "the guest never drew into the scanout resource" from "crosvm's
// ctx0 readback of a drawn resource is black". Raw ioctls only (no libdrm).
//   gcc -O2 -o /tmp/gvm-fbread gvm-fbread.c && /tmp/gvm-fbread [/dev/dri/card0]
// Output: "FBREAD fb=<id> res=<hw res> <w>x<h> pitch=<p> fmt=<fourcc> mean=<0..1> nonzero=<0..1>" or "FBREAD-ERR ...".
#include <errno.h>
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <unistd.h>
#include <drm/drm.h>
#include <drm/drm_mode.h>
#include <drm/virtgpu_drm.h>

static int xioctl(int fd, unsigned long req, void *arg)
{
	int r;
	do r = ioctl(fd, req, arg); while (r == -1 && (errno == EINTR || errno == EAGAIN));
	return r;
}

int main(int argc, char **argv)
{
	const char *dev = argc > 1 ? argv[1] : "/dev/dri/card0";
	int fd = open(dev, O_RDWR | O_CLOEXEC);
	if (fd < 0) { printf("FBREAD-ERR open %s: %s\n", dev, strerror(errno)); return 1; }
	uint32_t crtcs[32];
	struct drm_mode_card_res res = {0};
	if (xioctl(fd, DRM_IOCTL_MODE_GETRESOURCES, &res)) { printf("FBREAD-ERR getresources: %s\n", strerror(errno)); return 1; }
	if (res.count_crtcs > 32) res.count_crtcs = 32;
	struct drm_mode_card_res res2 = {0};
	res2.crtc_id_ptr = (uintptr_t)crtcs; res2.count_crtcs = res.count_crtcs;
	if (xioctl(fd, DRM_IOCTL_MODE_GETRESOURCES, &res2)) { printf("FBREAD-ERR getresources2: %s\n", strerror(errno)); return 1; }
	int found = 0;
	for (uint32_t i = 0; i < res2.count_crtcs; i++) {
		struct drm_mode_crtc c = { .crtc_id = crtcs[i] };
		if (xioctl(fd, DRM_IOCTL_MODE_GETCRTC, &c) || !c.fb_id) continue;
		found = 1;
		struct drm_mode_fb_cmd2 fb = { .fb_id = c.fb_id };
		if (xioctl(fd, DRM_IOCTL_MODE_GETFB2, &fb)) { printf("FBREAD-ERR getfb2 %u: %s\n", c.fb_id, strerror(errno)); continue; }
		uint32_t h = fb.handles[0], pitch = fb.pitches[0];
		struct drm_virtgpu_resource_info ri = { .bo_handle = h };
		if (xioctl(fd, DRM_IOCTL_VIRTGPU_RESOURCE_INFO, &ri)) { printf("FBREAD-ERR resource_info: %s\n", strerror(errno)); continue; }
		struct drm_virtgpu_3d_transfer_from_host t = {0};
		/* stride / layer_stride must be 0 for a non-blob resource (the kernel returns EINVAL otherwise:
		 * v10c read "transfer=Invalid argument" and showed the untouched guest backing) */
		t.bo_handle = h; t.box.w = fb.width; t.box.h = fb.height; t.box.d = 1; t.stride = 0;
		int terr = xioctl(fd, DRM_IOCTL_VIRTGPU_TRANSFER_FROM_HOST, &t) ? errno : 0;
		struct drm_virtgpu_3d_wait w = { .handle = h };
		xioctl(fd, DRM_IOCTL_VIRTGPU_WAIT, &w);
		struct drm_virtgpu_map m = { .handle = h };
		if (xioctl(fd, DRM_IOCTL_VIRTGPU_MAP, &m)) { printf("FBREAD-ERR map: %s\n", strerror(errno)); continue; }
		size_t len = (size_t)pitch * fb.height;
		uint8_t *p = mmap(NULL, len, PROT_READ, MAP_SHARED, fd, m.offset);
		if (p == MAP_FAILED) { printf("FBREAD-ERR mmap: %s\n", strerror(errno)); continue; }
		uint64_t sum = 0, nz = 0, n = 0;
		for (uint32_t y = 0; y < fb.height; y += 4)
			for (uint32_t x = 0; x < fb.width; x += 4) {
				const uint8_t *px = p + (size_t)y * pitch + (size_t)x * 4;
				unsigned v = px[0] + px[1] + px[2];
				sum += v; nz += v != 0; n++;
			}
		char f[5] = { fb.pixel_format, fb.pixel_format >> 8, fb.pixel_format >> 16, fb.pixel_format >> 24, 0 };
		printf("FBREAD crtc=%u fb=%u res=%u %ux%u pitch=%u fmt=%s mod=0x%llx transfer=%s mean=%.3f nonzero=%.3f\n",
		       c.crtc_id, c.fb_id, ri.res_handle, fb.width, fb.height, pitch, f,
		       (unsigned long long)fb.modifier[0], terr ? strerror(terr) : "ok",
		       n ? (double)sum / (n * 765.0) : 0.0, n ? (double)nz / n : 0.0);
		munmap(p, len);
	}
	if (!found) printf("FBREAD-ERR no CRTC with a framebuffer\n");
	return 0;
}
