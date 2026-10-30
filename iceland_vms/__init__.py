"""iceland-vms: virtual machines on the OnePlus Pad 4 (Gunyah hypervisor, crosvm protected VMs).

The package has no GTK imports outside iceland_vms.ui, so the root helper can import the
shared model (config, crosvmargs, host) without loading the toolkit.
"""
import os

VERSION = "0.2-26"
APP_ID = "org.iceland.VirtualMachines"
MALLOC_ARENAS = 2


def limit_malloc_arenas(n=MALLOC_ARENAS):
    """At most n glibc malloc arenas for this process (M_ARENA_MAX). glibc gives threads that allocate at the same
    time arenas of their own; GTK's, Mesa's and our worker threads kept ~70 MiB of mostly free arena memory in the
    window (its PSS falls from about 155 to 87 MiB with 2 arenas). Call it before other threads start; an explicit
    MALLOC_ARENA_MAX in the environment wins. Returns True when the limit was set."""
    if os.environ.get("MALLOC_ARENA_MAX"):
        return False
    try:
        import ctypes
        return ctypes.CDLL("libc.so.6", use_errno=True).mallopt(-8, int(n)) == 1     # -8 = M_ARENA_MAX
    except (OSError, AttributeError, ValueError):
        return False
