#!/bin/sh
# Install the tablet's guest kernel (CONFIG_DMA_RESTRICTED_POOL, virtio through the shared swiotlb)
# INTO this guest, so it also boots through UEFI with the guest kernel instead of the distribution's.
# Use it when the distribution's kernel lacks CONFIG_DMA_RESTRICTED_POOL (e.g. Fedora 44) and you
# want UEFI boot, or to switch a direct-kernel VM to UEFI boot. In the guest, as root:
#   mount -t 9p -o trans=virtio,ro gvmshare /mnt && sh /mnt/gvm-guest-kernel-install.sh
# Undo: remove /boot/vmlinuz-<ver> and its boot entry (kernel-install remove <ver>, or update-grub).
set -e
d=$(dirname "$0")
t=$(ls "$d"/modules-*.tar.gz | head -n1)
[ -f "$t" ] && [ -f "$d/Image" ] || { echo "no guest kernel in $d"; exit 1; }
ver=$(basename "$t" .tar.gz); ver=${ver#modules-}
if [ -L /lib ]; then tar -C /usr -xzf "$t"; else tar --keep-directory-symlink -C / -xzf "$t"; fi
mkdir -p "/lib/modules/$ver/extra"
# gunyah_guest is built in since 24 Oct; the .ko only for a kernel without it
if grep -q gunyah_guest "/lib/modules/$ver/modules.builtin" 2>/dev/null; then rm -f "/lib/modules/$ver/extra/gunyah_guest.ko"
else cp "$d"/extra/*.ko "/lib/modules/$ver/extra/" 2>/dev/null || true; fi
depmod -a "$ver"
command -v restorecon >/dev/null && restorecon -R "/lib/modules/$ver" || true
if command -v kernel-install >/dev/null; then
	kernel-install add "$ver" "$d/Image"                  # Fedora / BLS: also makes an initramfs
	command -v grubby >/dev/null && grubby --set-default "/boot/vmlinuz-$ver" || true
else
	install -m 644 "$d/Image" "/boot/vmlinuz-$ver"
	command -v update-initramfs >/dev/null && update-initramfs -c -k "$ver" || true
	command -v update-grub >/dev/null && update-grub || true
	command -v grub-mkconfig >/dev/null && [ -d /boot/grub ] && grub-mkconfig -o /boot/grub/grub.cfg || true
fi
echo "guest kernel $ver installed; it is the default at the next UEFI boot"
