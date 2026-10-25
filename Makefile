# iceland-vms - GNOME app for protected Gunyah VMs. `make install DESTDIR=...` (used by the rpm), `make check`.
PREFIX ?= /usr
SHARE = $(DESTDIR)$(PREFIX)/share/iceland-vms
FW ?= fw/out

all:

check:
	python3 -m py_compile iceland_vms/*.py iceland_vms/ui/*.py bin/iceland-vms bin/iceland-vms-helper tools/vm-iso-test.py
	sh -n data/guest-tools/gvm-agent data/guest-tools/*.sh data/iceland-vms.sleep
	python3 -m unittest discover -s tests -v

install:
	install -d $(SHARE)/lib/iceland_vms/ui $(SHARE)/firmware $(SHARE)/guest-tools
	install -m 644 iceland_vms/*.py $(SHARE)/lib/iceland_vms/
	install -m 644 iceland_vms/ui/*.py $(SHARE)/lib/iceland_vms/ui/
	install -D -m 755 tools/vm-iso-test.py $(SHARE)/tests/vm-iso-test.py
	install -m 644 data/catalogue.json data/features.json data/org.iceland.VirtualMachines.autostart.desktop $(SHARE)/
	install -m 755 data/guest-tools/gvm-agent data/guest-tools/*.sh $(SHARE)/guest-tools/
	install -m 644 data/guest-tools/gvm-agent.service $(SHARE)/guest-tools/
	install -m 644 $(FW)/edk2-gunyah-cam-release-noacpi.fd $(SHARE)/firmware/edk2-gunyah.fd
	install -m 644 $(FW)/edk2-gunyah-cam-debug.fd $(SHARE)/firmware/edk2-gunyah-debug.fd
	install -m 644 $(FW)/SHA256SUMS $(SHARE)/firmware/SHA256SUMS.build
	install -m 644 $(FW)/edk2-License.txt $(FW)/edk2-gunyah-LICENSE $(SHARE)/firmware/
	install -D -m 755 bin/iceland-vms $(DESTDIR)$(PREFIX)/bin/iceland-vms
	install -D -m 755 bin/iceland-vms-helper $(DESTDIR)$(PREFIX)/libexec/iceland-vms/iceland-vms-helper
	install -D -m 644 data/org.iceland.VirtualMachines.desktop $(DESTDIR)$(PREFIX)/share/applications/org.iceland.VirtualMachines.desktop
	install -D -m 644 data/org.iceland.VirtualMachines.metainfo.xml $(DESTDIR)$(PREFIX)/share/metainfo/org.iceland.VirtualMachines.metainfo.xml
	install -D -m 644 data/icons/org.iceland.VirtualMachines.svg $(DESTDIR)$(PREFIX)/share/icons/hicolor/scalable/apps/org.iceland.VirtualMachines.svg
	install -D -m 644 data/org.iceland.vms.policy $(DESTDIR)$(PREFIX)/share/polkit-1/actions/org.iceland.vms.policy
	install -D -m 755 data/iceland-vms.sleep $(DESTDIR)$(PREFIX)/lib/systemd/system-sleep/iceland-vms
	install -D -m 644 data/49-iceland-vms-wheel.rules.example $(DESTDIR)$(PREFIX)/share/doc/iceland-vms/49-iceland-vms-wheel.rules.example
	install -D -m 644 README.md $(DESTDIR)$(PREFIX)/share/doc/iceland-vms/README.md

.PHONY: all check install
