#!/bin/sh
# Install the iceland-vms guest agent by hand (guests installed from an ISO, without cloud-init).
# In the guest, as root:  mount -t 9p -o trans=virtio,ro gvmshare /mnt && sh /mnt/install-gvm-agent.sh
set -e
d=$(dirname "$0")
install -m 755 "$d/gvm-agent" /usr/local/sbin/gvm-agent
install -m 644 "$d/gvm-agent.service" /etc/systemd/system/gvm-agent.service
systemctl mask --now serial-getty@hvc0.service 2>/dev/null || true
systemctl daemon-reload
systemctl enable --now gvm-agent.service
echo "gvm-agent installed: the VM app can now shut this guest down cleanly"
