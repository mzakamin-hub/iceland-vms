"""iceland-vms: virtual machines on the OnePlus Pad 4 (Gunyah hypervisor, crosvm protected VMs).

The package has no GTK imports outside iceland_vms.ui, so the root helper can import the
shared model (config, crosvmargs, host) without loading the toolkit.
"""
VERSION = "0.1"
APP_ID = "org.iceland.VirtualMachines"
