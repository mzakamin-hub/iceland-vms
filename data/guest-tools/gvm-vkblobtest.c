/* SPDX-License-Identifier: GPL-2.0-only
 * gvm-vkblobtest: exercise host-visible virtio-gpu blob memory through Vulkan (venus) (V4-5 with GVM_VENUS=1).
 *
 * Venus backs every HOST_VISIBLE device-memory allocation with a host blob (VIRTGPU_BLOB_MEM_HOST3D) that the
 * guest maps; in a protected Gunyah VM each mapping goes through crosvm's runtime GuestAccept share. The
 * program allocates two HOST_VISIBLE|HOST_COHERENT buffers, writes a pattern through the CPU mapping of the
 * first, lets the GPU copy it into the second (vkCmdCopyBuffer) and verifies the second through its mapping,
 * several sizes and rounds. Prints "VKBLOBTEST PASS ..." or "VKBLOBTEST FAIL ...".
 *
 * Build in the guest:  gcc -O2 -o gvm-vkblobtest gvm-vkblobtest.c -lvulkan
 * Run:  ./gvm-vkblobtest [rounds] [--allow-cpu]   (--allow-cpu also accepts a CPU device, for PC tests)
 */
#include <vulkan/vulkan.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define CHECK(x, what) do { VkResult r_ = (x); if (r_ != VK_SUCCESS) { \
	printf("VKBLOBTEST FAIL %s: VkResult %d\n", what, r_); return 1; } } while (0)

static int find_mem(const VkPhysicalDeviceMemoryProperties *mp, uint32_t bits, VkMemoryPropertyFlags want)
{
	for (uint32_t i = 0; i < mp->memoryTypeCount; i++)
		if ((bits & (1u << i)) && (mp->memoryTypes[i].propertyFlags & want) == want)
			return (int)i;
	return -1;
}

struct buf { VkBuffer b; VkDeviceMemory m; uint32_t *p; };

static int make_buf(VkDevice dev, const VkPhysicalDeviceMemoryProperties *mp, VkDeviceSize n,
		    VkBufferUsageFlags usage, struct buf *o)
{
	VkBufferCreateInfo bi = { .sType = VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO, .size = n, .usage = usage,
				  .sharingMode = VK_SHARING_MODE_EXCLUSIVE };
	CHECK(vkCreateBuffer(dev, &bi, NULL, &o->b), "vkCreateBuffer");
	VkMemoryRequirements mr;
	vkGetBufferMemoryRequirements(dev, o->b, &mr);
	int t = find_mem(mp, mr.memoryTypeBits, VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT | VK_MEMORY_PROPERTY_HOST_COHERENT_BIT);
	if (t < 0) { puts("VKBLOBTEST FAIL no HOST_VISIBLE|HOST_COHERENT memory type"); return 1; }
	VkMemoryAllocateInfo ai = { .sType = VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO, .allocationSize = mr.size,
				    .memoryTypeIndex = (uint32_t)t };
	CHECK(vkAllocateMemory(dev, &ai, NULL, &o->m), "vkAllocateMemory (host-visible)");
	CHECK(vkBindBufferMemory(dev, o->b, o->m, 0), "vkBindBufferMemory");
	CHECK(vkMapMemory(dev, o->m, 0, VK_WHOLE_SIZE, 0, (void **)&o->p), "vkMapMemory (host-visible blob map)");
	return 0;
}

int main(int argc, char **argv)
{
	int rounds = argc > 1 ? atoi(argv[1]) : 8, allow_cpu = argc > 2 && !strcmp(argv[2], "--allow-cpu");
	if (rounds < 1)
		rounds = 1;
	VkApplicationInfo app = { .sType = VK_STRUCTURE_TYPE_APPLICATION_INFO, .pApplicationName = "gvm-vkblobtest",
				  .apiVersion = VK_API_VERSION_1_1 };
	VkInstanceCreateInfo ici = { .sType = VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO, .pApplicationInfo = &app };
	VkInstance inst;
	CHECK(vkCreateInstance(&ici, NULL, &inst), "vkCreateInstance");
	uint32_t n = 0;
	vkEnumeratePhysicalDevices(inst, &n, NULL);
	VkPhysicalDevice pds[16];
	if (n > 16) n = 16;
	vkEnumeratePhysicalDevices(inst, &n, pds);
	VkPhysicalDevice pd = VK_NULL_HANDLE;
	VkPhysicalDeviceProperties props;
	for (uint32_t i = 0; i < n; i++) {
		vkGetPhysicalDeviceProperties(pds[i], &props);
		if (props.deviceType != VK_PHYSICAL_DEVICE_TYPE_CPU || allow_cpu) { pd = pds[i]; break; }
	}
	if (pd == VK_NULL_HANDLE) { puts("VKBLOBTEST FAIL no GPU Vulkan device (venus not active?)"); return 2; }
	printf("device: %s (driver version 0x%x)\n", props.deviceName, props.driverVersion);
	uint32_t qn = 0;
	vkGetPhysicalDeviceQueueFamilyProperties(pd, &qn, NULL);
	VkQueueFamilyProperties qf[16];
	if (qn > 16) qn = 16;
	vkGetPhysicalDeviceQueueFamilyProperties(pd, &qn, qf);
	uint32_t q = UINT32_MAX;
	for (uint32_t i = 0; i < qn; i++)
		if (qf[i].queueFlags & (VK_QUEUE_GRAPHICS_BIT | VK_QUEUE_COMPUTE_BIT | VK_QUEUE_TRANSFER_BIT)) { q = i; break; }
	if (q == UINT32_MAX) { puts("VKBLOBTEST FAIL no queue family"); return 1; }
	float prio = 1.0f;
	VkDeviceQueueCreateInfo qci = { .sType = VK_STRUCTURE_TYPE_DEVICE_QUEUE_CREATE_INFO, .queueFamilyIndex = q,
					.queueCount = 1, .pQueuePriorities = &prio };
	VkDeviceCreateInfo dci = { .sType = VK_STRUCTURE_TYPE_DEVICE_CREATE_INFO, .queueCreateInfoCount = 1,
				   .pQueueCreateInfos = &qci };
	VkDevice dev;
	CHECK(vkCreateDevice(pd, &dci, NULL, &dev), "vkCreateDevice");
	VkQueue queue;
	vkGetDeviceQueue(dev, q, 0, &queue);
	VkPhysicalDeviceMemoryProperties mp;
	vkGetPhysicalDeviceMemoryProperties(pd, &mp);
	VkCommandPoolCreateInfo pci = { .sType = VK_STRUCTURE_TYPE_COMMAND_POOL_CREATE_INFO, .queueFamilyIndex = q,
					.flags = VK_COMMAND_POOL_CREATE_RESET_COMMAND_BUFFER_BIT };
	VkCommandPool pool;
	CHECK(vkCreateCommandPool(dev, &pci, NULL, &pool), "vkCreateCommandPool");
	VkCommandBufferAllocateInfo cai = { .sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO, .commandPool = pool,
					    .level = VK_COMMAND_BUFFER_LEVEL_PRIMARY, .commandBufferCount = 1 };
	VkCommandBuffer cb;
	CHECK(vkAllocateCommandBuffers(dev, &cai, &cb), "vkAllocateCommandBuffers");
	VkFenceCreateInfo fci = { .sType = VK_STRUCTURE_TYPE_FENCE_CREATE_INFO };
	VkFence fence;
	CHECK(vkCreateFence(dev, &fci, NULL, &fence), "vkCreateFence");

	static const VkDeviceSize sizes[] = { 64 << 10, 1 << 20, 16 << 20 };
	unsigned long long bytes = 0;
	for (unsigned s = 0; s < sizeof(sizes) / sizeof(sizes[0]); s++) {
		VkDeviceSize sz = sizes[s];
		size_t words = (size_t)(sz / 4);
		struct buf a, b;
		if (make_buf(dev, &mp, sz, VK_BUFFER_USAGE_TRANSFER_SRC_BIT, &a) ||
		    make_buf(dev, &mp, sz, VK_BUFFER_USAGE_TRANSFER_DST_BIT, &b))
			return 1;
		for (int r = 0; r < rounds; r++) {
			for (size_t i = 0; i < words; i++)
				a.p[i] = (uint32_t)(i * 2654435761u) ^ (uint32_t)(r * 0x9e3779b9u) ^ (uint32_t)sz;
			memset(b.p, 0, (size_t)sz);
			VkCommandBufferBeginInfo bi = { .sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO,
							.flags = VK_COMMAND_BUFFER_USAGE_ONE_TIME_SUBMIT_BIT };
			CHECK(vkResetCommandBuffer(cb, 0), "vkResetCommandBuffer");
			CHECK(vkBeginCommandBuffer(cb, &bi), "vkBeginCommandBuffer");
			VkBufferCopy region = { 0, 0, sz };
			vkCmdCopyBuffer(cb, a.b, b.b, 1, &region);
			VkMemoryBarrier mb = { .sType = VK_STRUCTURE_TYPE_MEMORY_BARRIER,
					       .srcAccessMask = VK_ACCESS_TRANSFER_WRITE_BIT, .dstAccessMask = VK_ACCESS_HOST_READ_BIT };
			vkCmdPipelineBarrier(cb, VK_PIPELINE_STAGE_TRANSFER_BIT, VK_PIPELINE_STAGE_HOST_BIT, 0, 1, &mb, 0, NULL, 0, NULL);
			CHECK(vkEndCommandBuffer(cb), "vkEndCommandBuffer");
			VkSubmitInfo si = { .sType = VK_STRUCTURE_TYPE_SUBMIT_INFO, .commandBufferCount = 1, .pCommandBuffers = &cb };
			CHECK(vkResetFences(dev, 1, &fence), "vkResetFences");
			CHECK(vkQueueSubmit(queue, 1, &si, fence), "vkQueueSubmit");
			CHECK(vkWaitForFences(dev, 1, &fence, VK_TRUE, 10ull * 1000 * 1000 * 1000), "vkWaitForFences");
			for (size_t i = 0; i < words; i++) {
				uint32_t want = (uint32_t)(i * 2654435761u) ^ (uint32_t)(r * 0x9e3779b9u) ^ (uint32_t)sz;
				if (b.p[i] != want) {
					printf("VKBLOBTEST FAIL size %llu round %d word %zu: 0x%08x != 0x%08x\n",
					       (unsigned long long)sz, r, i, b.p[i], want);
					return 1;
				}
			}
			bytes += sz;
		}
		vkUnmapMemory(dev, a.m);
		vkUnmapMemory(dev, b.m);
		vkDestroyBuffer(dev, a.b, NULL);
		vkDestroyBuffer(dev, b.b, NULL);
		vkFreeMemory(dev, a.m, NULL);
		vkFreeMemory(dev, b.m, NULL);
	}
	printf("VKBLOBTEST PASS %llu MiB copied through host-visible coherent memory on %s (%d rounds x 3 sizes)\n",
	       bytes >> 20, props.deviceName, rounds);
	vkDestroyFence(dev, fence, NULL);
	vkDestroyCommandPool(dev, pool, NULL);
	vkDestroyDevice(dev, NULL);
	vkDestroyInstance(inst, NULL);
	return 0;
}
