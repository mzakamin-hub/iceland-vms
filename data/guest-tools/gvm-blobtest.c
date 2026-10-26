/* SPDX-License-Identifier: GPL-2.0-only
 * gvm-blobtest: exercise host-visible virtio-gpu blob memory from a guest GL context (V4-5).
 *
 * Mesa's virgl driver backs GL buffers created with GL_MAP_PERSISTENT_BIT | GL_MAP_COHERENT_BIT by
 * host-visible blob resources (VIRTGPU_BLOB_MEM_HOST3D, mappable). In a protected Gunyah VM every such
 * mapping goes through crosvm's runtime GuestAccept share (CROSVM_GVM_ACCEPT=1). This program creates
 * two persistent coherent buffers, writes a pattern through the CPU mapping of the first, lets the GPU
 * copy it into the second (glCopyBufferSubData) and checks the result through the CPU mapping of the
 * second, several rounds and sizes. Prints "BLOBTEST PASS ..." or "BLOBTEST FAIL ...".
 *
 * Build in the guest:  gcc -O2 -o gvm-blobtest gvm-blobtest.c -lEGL -lOpenGL
 * Run with no display server holding the GPU:  EGL_PLATFORM=surfaceless ./gvm-blobtest
 */
#define GL_GLEXT_PROTOTYPES 1
#define EGL_EGLEXT_PROTOTYPES 1
#include <EGL/egl.h>
#include <EGL/eglext.h>
#include <GL/gl.h>
#include <GL/glext.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#ifndef EGL_PLATFORM_SURFACELESS_MESA
#define EGL_PLATFORM_SURFACELESS_MESA 0x31DD
#endif

static int has_ext(const char *name)
{
	GLint n = 0;
	glGetIntegerv(GL_NUM_EXTENSIONS, &n);
	for (GLint i = 0; i < n; i++) {
		const char *e = (const char *)glGetStringi(GL_EXTENSIONS, (GLuint)i);
		if (e && !strcmp(e, name))
			return 1;
	}
	return 0;
}

int main(int argc, char **argv)
{
	int rounds = argc > 1 ? atoi(argv[1]) : 8;
	PFNEGLGETPLATFORMDISPLAYEXTPROC gpd =
		(PFNEGLGETPLATFORMDISPLAYEXTPROC)eglGetProcAddress("eglGetPlatformDisplayEXT");
	EGLDisplay dpy = gpd ? gpd(EGL_PLATFORM_SURFACELESS_MESA, EGL_DEFAULT_DISPLAY, NULL) : EGL_NO_DISPLAY;
	if (dpy == EGL_NO_DISPLAY || !eglInitialize(dpy, NULL, NULL)) {
		puts("BLOBTEST FAIL no surfaceless EGL display");
		return 2;
	}
	eglBindAPI(EGL_OPENGL_API);
	static const int versions[][2] = { { 4, 4 }, { 4, 3 }, { 3, 3 } };
	EGLContext ctx = EGL_NO_CONTEXT;
	for (unsigned v = 0; v < 3 && ctx == EGL_NO_CONTEXT; v++) {
		const EGLint attrs[] = { EGL_CONTEXT_MAJOR_VERSION, versions[v][0], EGL_CONTEXT_MINOR_VERSION, versions[v][1],
					 EGL_CONTEXT_OPENGL_PROFILE_MASK, EGL_CONTEXT_OPENGL_CORE_PROFILE_BIT, EGL_NONE };
		ctx = eglCreateContext(dpy, EGL_NO_CONFIG_KHR, EGL_NO_CONTEXT, attrs);
	}
	if (ctx == EGL_NO_CONTEXT || !eglMakeCurrent(dpy, EGL_NO_SURFACE, EGL_NO_SURFACE, ctx)) {
		puts("BLOBTEST FAIL no GL core context");
		return 2;
	}
	printf("renderer: %s\nversion: %s\n", glGetString(GL_RENDERER), glGetString(GL_VERSION));
	GLint maj = 0, min = 0;
	glGetIntegerv(GL_MAJOR_VERSION, &maj);
	glGetIntegerv(GL_MINOR_VERSION, &min);
	if (maj * 10 + min < 44 && !has_ext("GL_ARB_buffer_storage")) {
		puts("BLOBTEST FAIL no buffer storage (GL 4.4 / GL_ARB_buffer_storage)");
		return 2;
	}
	const GLbitfield flags = GL_MAP_PERSISTENT_BIT | GL_MAP_COHERENT_BIT;
	static const size_t sizes[] = { 64 << 10, 1 << 20, 16 << 20 };
	unsigned long long bytes = 0;
	for (unsigned s = 0; s < sizeof(sizes) / sizeof(sizes[0]); s++) {
		size_t n = sizes[s], words = n / 4;
		GLuint b[2];
		glGenBuffers(2, b);
		glBindBuffer(GL_COPY_READ_BUFFER, b[0]);
		glBufferStorage(GL_COPY_READ_BUFFER, (GLsizeiptr)n, NULL, GL_MAP_WRITE_BIT | flags);
		uint32_t *src = glMapBufferRange(GL_COPY_READ_BUFFER, 0, (GLsizeiptr)n, GL_MAP_WRITE_BIT | flags);
		glBindBuffer(GL_COPY_WRITE_BUFFER, b[1]);
		glBufferStorage(GL_COPY_WRITE_BUFFER, (GLsizeiptr)n, NULL, GL_MAP_READ_BIT | flags);
		const uint32_t *dst = glMapBufferRange(GL_COPY_WRITE_BUFFER, 0, (GLsizeiptr)n, GL_MAP_READ_BIT | flags);
		if (!src || !dst) {
			printf("BLOBTEST FAIL persistent coherent map of %zu bytes failed (GL error 0x%x)\n", n, glGetError());
			return 1;
		}
		for (int r = 0; r < rounds; r++) {
			for (size_t i = 0; i < words; i++)
				src[i] = (uint32_t)(i * 2654435761u) ^ (uint32_t)(r * 0x9e3779b9u) ^ (uint32_t)n;
			glCopyBufferSubData(GL_COPY_READ_BUFFER, GL_COPY_WRITE_BUFFER, 0, 0, (GLsizeiptr)n);
			glFinish();
			for (size_t i = 0; i < words; i++) {
				uint32_t want = (uint32_t)(i * 2654435761u) ^ (uint32_t)(r * 0x9e3779b9u) ^ (uint32_t)n;
				if (dst[i] != want) {
					printf("BLOBTEST FAIL size %zu round %d word %zu: 0x%08x != 0x%08x\n", n, r, i, dst[i], want);
					return 1;
				}
			}
			bytes += n;
		}
		glUnmapBuffer(GL_COPY_WRITE_BUFFER);
		glBindBuffer(GL_COPY_READ_BUFFER, b[0]);
		glUnmapBuffer(GL_COPY_READ_BUFFER);
		glDeleteBuffers(2, b);
	}
	GLenum err = glGetError();
	if (err != GL_NO_ERROR) {
		printf("BLOBTEST FAIL GL error 0x%x\n", err);
		return 1;
	}
	printf("BLOBTEST PASS %llu MiB copied through persistent coherent buffers (%d rounds x 3 sizes)\n",
	       bytes >> 20, rounds);
	eglMakeCurrent(dpy, EGL_NO_SURFACE, EGL_NO_SURFACE, EGL_NO_CONTEXT);
	eglDestroyContext(dpy, ctx);
	eglTerminate(dpy);
	return 0;
}
