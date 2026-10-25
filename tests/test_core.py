"""Unit tests (PC): pure logic only - no VM is started, nothing touches Gunyah."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from iceland_vms import config, crosvmargs, imageprobe  # noqa: E402

FEDORA_RAW = os.path.expanduser("~/mainline/build/wf/gvm/image/fedora44-cloud.raw")


def fedora_cfg():
    c = config.default_config("fedora-44", "Fedora 44")
    c["boot"].update(root="PARTUUID=51293621-3df4-4df4-95a5-16321a399417", rootfstype="btrfs", rootflags="subvol=root")
    c["disks"] = [{"store": "vm", "name": "disk0.raw", "kind": "disk", "ro": False}]
    c["cloud_init"]["enabled"] = True
    c["network"]["mac"] = "0a:1c:00:00:00:01"
    return c


RES = {"control_sock": "/run/iceland-vms/u1000/fedora-44/crosvm.sock",
       "serial_sock": "/run/iceland-vms/u1000/fedora-44/serial.sock",
       "agent_sock": "/run/iceland-vms/u1000/fedora-44/agent.sock",
       "kernel": "/usr/libexec/iceland-gvm/guest/Image", "initrd": "/usr/libexec/iceland-gvm/guest/initramfs-gvm.cpio.gz",
       "firmware": "/usr/share/iceland-vms/firmware/edk2-gunyah.fd", "disks": ["/proc/self/fd/5"], "seed": "/proc/self/fd/6",
       "tools_dir": "/run/iceland-vms/u1000/fedora-44/tools", "tap": "gvmtap0",
       "wayland_sock": "/run/user/1000/wayland-0", "cpu_sets": {"all": list(range(8)), "big": [6, 7], "little": list(range(6))}}


class ConfigTests(unittest.TestCase):
    def test_default_valid(self):
        config.validate(fedora_cfg())

    def test_rejects_bad_values(self):
        bad = [
            ("id", "../etc"), ("cpus", 0), ("cpus", 99), ("memory_mib", 1 << 20), ("backend", "kvm"),
        ]
        for k, v in bad:
            c = fedora_cfg()
            c[k] = v
            with self.assertRaises(config.ConfigError, msg=k):
                config.validate(c)
        c = fedora_cfg()
        c["disks"][0]["name"] = "../../etc/shadow"
        self.assertRaises(config.ConfigError, config.validate, c)
        c = fedora_cfg()
        c["disks"].append({"store": "isos", "name": "x.iso", "kind": "cdrom", "ro": False})
        self.assertRaises(config.ConfigError, config.validate, c)
        c = fedora_cfg()
        c["boot"]["cmdline_extra"] = "init=/bin/sh; rm -rf /"
        self.assertRaises(config.ConfigError, config.validate, c)
        c = fedora_cfg()
        c["network"]["forwards"] = [{"proto": "tcp", "host": 22, "guest": 22}]
        self.assertRaises(config.ConfigError, config.validate, c)
        c = fedora_cfg()
        c["shares"] = [{"path": "/home/u/../../etc", "tag": "s", "kind": "p9", "ro": True}]
        self.assertRaises(config.ConfigError, config.validate, c)
        c = fedora_cfg()
        c["usb"] = [{"vendor": "0781", "product": "5581"}]
        c["backend"] = "crosvm"
        self.assertRaises(config.ConfigError, config.validate, c)      # USB never with crosvm
        c["backend"] = "auto"
        config.validate(c)                                              # auto would pick qemu ...
        self.assertRaises(config.ConfigError, config.validate, c, {"qemu_backend": False})   # ... if it existed

    def test_backend_auto(self):
        c = fedora_cfg()
        self.assertEqual(config.resolve_backend(c, {"qemu_backend": True}), "crosvm")
        c["usb"] = [{"vendor": "0781", "product": "5581"}]
        self.assertEqual(config.resolve_backend(c, {"qemu_backend": True}), "qemu")
        self.assertEqual(config.resolve_backend(c, {"qemu_backend": False}), "crosvm")
        c["display"]["gpu"] = "virgl"
        self.assertIn("3D graphics", config.backend_conflicts(c, "qemu"))
        self.assertEqual(config.backend_conflicts(dict(c, usb=[]), "crosvm"), [])

    def test_feature_gates(self):
        c = fedora_cfg()
        c["display"]["gpu"] = "virgl"
        config.validate(c)                                              # no gate without features
        self.assertRaises(config.ConfigError, config.validate, c, {"gpu3d_virgl": False})
        config.validate(c, {"gpu3d_virgl": True, "uefi": True})

    def test_cmdline_has_no_uuid_root(self):
        cl = config.kernel_cmdline(fedora_cfg())
        self.assertIn("root=PARTUUID=51293621-3df4-4df4-95a5-16321a399417", cl)
        self.assertIn("rootflags=subvol=root", cl)
        self.assertNotIn("root=UUID", cl)
        self.assertTrue(cl.index("console=tty0") < cl.index("console=ttyS0"))   # ttyS0 stays /dev/console


class CrosvmArgsTests(unittest.TestCase):
    def test_proven_base_line(self):
        c2d = fedora_cfg()
        c2d["display"]["gpu"] = "2d"                  # the default is 3D (virgl + venus) since 0.2-4
        a, env = crosvmargs.build(c2d, RES)
        s = " ".join(a)
        cs = fedora_cfg()
        cs["display"]["gpu"] = "simplefb"
        self.assertIn("--simplefb width=1600,height=1000", " ".join(crosvmargs.build(cs, RES)[0]))
        # the V1 / V1b proven prefix (gvm.sh base_args)
        self.assertTrue(s.startswith("run --disable-sandbox --hypervisor gunyah --protected-vm-without-firmware "
                                     "--no-usb --no-balloon --cpus 4 --mem 2048 --swiotlb 128 -s "))
        self.assertIn("--block /proc/self/fd/5 ", s)
        self.assertIn("--block /proc/self/fd/6,ro=true", s)
        self.assertIn("--net tap-name=gvmtap0,mac=0a:1c:00:00:00:01", s)
        self.assertIn("hardware=virtio-console,num=1", s)
        self.assertIn("--gpu backend=2d,displays=[[mode=windowed[1600,1000]]]", s)
        self.assertIn("--wayland-sock /run/user/1000/wayland-0", s)
        self.assertEqual(a[-1], RES["kernel"])
        self.assertEqual(a[-3], "-p")
        self.assertEqual(env, {})

    def test_uefi_and_3d(self):
        c = fedora_cfg()
        c["boot"]["mode"] = "uefi"
        c["display"]["gpu"] = "native"
        c["display"]["dpi"] = 160
        a, env = crosvmargs.build(c, RES)
        self.assertEqual(a[-1], RES["firmware"])
        self.assertNotIn("-p", a)
        self.assertEqual(env, {"CROSVM_GVM_ACCEPT": "1", "FD_MESA_DEBUG": "noubwc,notile", "CROSVM_GVM_SCANOUT_READ": "ctx0"})
        self.assertIn("context-types=drm,", " ".join(a))
        self.assertIn("dpi=[160,160]", " ".join(a))

    def test_affinity_and_shares(self):
        c = fedora_cfg()
        c["cpu_affinity"] = "big"
        a, _ = crosvmargs.build(c, dict(RES, shares=[("/run/x/share-s0", "s0", "fs")]))
        s = " ".join(a)
        self.assertIn("--cpu-affinity 6,7", s)
        self.assertIn("--shared-dir /run/x/share-s0:s0:type=fs", s)


@unittest.skipUnless(os.path.exists(FEDORA_RAW), "Fedora Cloud raw not on this PC")
class ProbeTests(unittest.TestCase):
    def test_fedora_cloud(self):
        r = imageprobe.probe(FEDORA_RAW)
        self.assertEqual(r["root"], "PARTUUID=51293621-3df4-4df4-95a5-16321a399417")
        self.assertEqual(r["rootfstype"], "btrfs")
        self.assertEqual(r["rootflags"], "subvol=root")


class SeedTests(unittest.TestCase):
    def test_user_data(self):
        try:
            import yaml
        except ImportError:
            self.skipTest("PyYAML missing")
        from iceland_vms import seed
        with tempfile.TemporaryDirectory() as home:
            c = fedora_cfg()
            os.makedirs(config.vm_dir(home, c["id"]))
            ud = seed.user_data(home, c)
            d = yaml.safe_load(ud)
            self.assertTrue(ud.startswith("#cloud-config"))
            self.assertEqual(d["users"][0]["name"], "gvm")
            boot = " ".join(x[2] for x in d["bootcmd"])
            self.assertIn("if [ -L /lib ]; then tar -C /usr", boot)        # V2 fix: never replace /lib
            self.assertNotIn("tar -C / -xzf \"$t\" &&", boot.split("else")[0])
            paths = [f["path"] for f in d["write_files"]]
            self.assertIn("/usr/local/sbin/gvm-agent", paths)


class SafeOpenTests(unittest.TestCase):
    def test_symlink_refused(self):
        from iceland_vms import helper
        with tempfile.TemporaryDirectory() as t:
            real = os.path.join(t, "real.raw")
            open(real, "w").close()
            link = os.path.join(t, "disk0.raw")
            os.symlink(real, link)
            fd = helper.open_nofollow(real, os.O_RDONLY, owner=os.getuid())
            os.close(fd)
            with self.assertRaises(helper.HelperError):
                helper.open_nofollow(link, os.O_RDONLY)
            with self.assertRaises(helper.HelperError):
                helper.open_nofollow(real, os.O_RDONLY, owner=os.getuid() + 1)
            os.mkfifo(os.path.join(t, "fifo"))
            with self.assertRaises(helper.HelperError):                    # no hang on a FIFO
                helper.open_nofollow(os.path.join(t, "fifo"), os.O_RDONLY)


if __name__ == "__main__":
    unittest.main()


class QemuArgsTests(unittest.TestCase):
    def test_protected_and_usb(self):
        from iceland_vms import qemuargs
        c = fedora_cfg()
        c["backend"] = "qemu"
        c["usb"] = [{"vendor": "0781", "product": "5581"}]
        a, env = qemuargs.build(c, RES)
        s = " ".join(a)
        self.assertIn("-accel gunyah,protected=on", s)                   # never an unprotected VM
        self.assertIn("swiotlb-size=128M", s)
        self.assertIn("-m 2176M", s)                                     # -m includes the swiotlb
        self.assertIn("usb-host,bus=xhci0.0,vendorid=0x0781,productid=0x5581", s)
        self.assertIn("console=ttyAMA0", a[a.index("-append") + 1])
        self.assertNotIn("console=ttyS0", a[a.index("-append") + 1])
        self.assertEqual(env.get("WAYLAND_DISPLAY"), "/run/user/1000/wayland-0")


class QemuDeviceBudgetTests(unittest.TestCase):
    def test_input_on_xhci(self):
        from iceland_vms import qemuargs
        c = fedora_cfg()
        c["backend"] = "qemu"
        c["hugepages"] = True
        a, _ = qemuargs.build(c, RES)
        s = " ".join(a)
        self.assertNotIn("virtio-keyboard-pci", s)                       # keyboard / pointer on xHCI
        self.assertIn("usb-kbd,bus=xhci0.0", s)
        self.assertEqual(s.count("qemu-xhci"), 1)
        self.assertIn("romfile=", s)
        self.assertNotIn("hugetlb=on", s)                                # Q1b: Gunyah cannot demand-page hugetlb
        self.assertEqual(qemuargs.hugepages_needed(c), 0)
        self.assertLessEqual(qemuargs.pci_devices(a), 9)


class OldConfigTests(unittest.TestCase):
    def test_save_fills_missing_keys(self):
        import json, tempfile
        c = fedora_cfg()
        del c["display"]["glamor"]                       # a vm.json written by 0.1-3
        config.validate(json.loads(json.dumps(c)))
        with tempfile.TemporaryDirectory() as d:
            raw = json.loads(json.dumps(c))
            config.save(raw, os.path.join(d, "vm.json"))
            self.assertIn("glamor", json.load(open(os.path.join(d, "vm.json")))["display"])


class CreatePathTests(unittest.TestCase):
    def test_create_uefi_iso_vm(self):
        from iceland_vms import create, storage
        with tempfile.TemporaryDirectory() as home:
            iso = os.path.join(home, "x.iso")
            with open(iso, "wb") as f:
                f.write(bytes(4096))
            cfg = create.create_vm(home, {"kind": "iso", "path": iso},
                                   dict(name="U1 test", cpus=4, mem=4096, disk=1, net=False, gpu="none"))
            self.assertEqual(cfg["boot"]["mode"], "uefi")
            self.assertEqual([d["kind"] for d in cfg["disks"]], ["cdrom", "disk"])
            self.assertTrue(os.path.exists(storage.disk_path(home, cfg, cfg["disks"][0])))
            self.assertEqual(os.path.getsize(storage.disk_path(home, cfg, cfg["disks"][1])), 1 << 30)
            config.validate(config.load(os.path.join(config.vm_dir(home, cfg["id"]), "vm.json")))


class SimplefbTests(unittest.TestCase):
    def test_simplefb_args(self):
        c = fedora_cfg()
        c["display"]["gpu"] = "simplefb"
        self.assertRaises(config.ConfigError, config.validate, c, {"simplefb": False})
        config.validate(c, {"simplefb": True})
        a, env = crosvmargs.gpu_args(c, "/run/user/1000/wayland-0", {})
        self.assertEqual(a[:2], ["--simplefb", "width=1600,height=1000"])
        self.assertNotIn("--gpu", a)
        self.assertEqual(env, {})
        self.assertIn("the shared-framebuffer software display", config.backend_conflicts(c, "qemu"))


class DisplayDefaultsTests(unittest.TestCase):
    def test_old_vm_json_display_migration_once(self):
        import json
        for gpu, want in (("venus", True), ("simplefb", False), ("2d", False)):
            c = fedora_cfg()
            c["display"].update(gpu=gpu, glamor=not want, session="wayland")
            c["display"].pop("defaults_rev", None)
            with tempfile.TemporaryDirectory() as d:
                p = os.path.join(d, "vm.json")
                json.dump(c, open(p, "w"))
                l1 = config.load(p)
                self.assertEqual((l1["display"]["gpu"], l1["display"]["glamor"], l1["display"]["session"]),
                                 (gpu, want, "x11"))
                l1["display"]["glamor"] = not want            # the user's own choice afterwards stays
                config.save(l1, p)
                self.assertEqual(config.load(p)["display"]["glamor"], not want)

    def test_new_vm_default_is_3d_composited(self):
        c = config.new_config("x") if hasattr(config, "new_config") else fedora_cfg()
        self.assertEqual((c["display"]["gpu"], c["display"]["glamor"]), ("venus", True))


class RebootDetectTests(unittest.TestCase):
    def test_last_reboot_line(self):
        import types
        from iceland_vms.helper import Console
        f = lambda b: Console.guest_rebooted(types.SimpleNamespace(since=bytearray(b)))  # noqa: E731
        self.assertTrue(f(b"...\r\n[  9.1] reboot: Restarting system\r\n"))
        self.assertFalse(f(b"[  9.1] reboot: Power down\r\n"))
        self.assertFalse(f(b"reboot: Restarting system\r\nboot...\r\nreboot: Power down\r\n"))
        self.assertFalse(f(b"no reboot line"))


class PortProxyTests(unittest.TestCase):
    def _free_port(self, kind):
        import socket
        s = socket.socket(socket.AF_INET, kind)
        s.bind(("127.0.0.1", 0))
        p = s.getsockname()[1]
        s.close()
        return p

    def test_tcp_and_udp(self):
        import socket
        import threading
        from iceland_vms import portproxy
        # TCP echo "guest"
        es = socket.socket()
        es.bind(("127.0.0.1", 0))
        es.listen(1)

        def echo():
            c, _ = es.accept()
            c.sendall(c.recv(100).upper())
            c.close()
        threading.Thread(target=echo, daemon=True).start()
        # UDP echo "guest"
        us = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        us.bind(("127.0.0.1", 0))

        def uecho():
            d, peer = us.recvfrom(100)
            us.sendto(d[::-1], peer)
        threading.Thread(target=uecho, daemon=True).start()
        tp, up = self._free_port(socket.SOCK_STREAM), self._free_port(socket.SOCK_DGRAM)
        fw = [{"proto": "tcp", "host": tp, "guest": es.getsockname()[1]},
              {"proto": "udp", "host": up, "guest": us.getsockname()[1]}]
        run = portproxy.start(fw, "127.0.0.1", log=lambda m: None)
        try:
            self.assertEqual(len(run), 2)
            c = socket.create_connection(("127.0.0.1", tp), timeout=5)
            c.sendall(b"hello")
            self.assertEqual(c.recv(100), b"HELLO")
            c.close()
            u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            u.settimeout(5)
            u.sendto(b"abc", ("127.0.0.1", up))
            self.assertEqual(u.recv(100), b"cba")
        finally:
            portproxy.stop(run)
