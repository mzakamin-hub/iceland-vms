"""0.2-23: msm native context - crosvm keeps vrend for the 2D console, the guest Mesa tarball lookup."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from iceland_vms import config  # noqa: E402


class GuestMesa(unittest.TestCase):
    def test_missing_dir(self):
        old = config.GUEST_MESA_DIR
        try:
            config.GUEST_MESA_DIR = "/nonexistent/guest-mesa"
            self.assertIsNone(config.guest_mesa_tarball())
        finally:
            config.GUEST_MESA_DIR = old

    def test_newest_tarball(self):
        old = config.GUEST_MESA_DIR
        with tempfile.TemporaryDirectory() as d:
            for n in ("nctx-mesa-26.2.5.tar.gz", "nctx-mesa-26.2.6.tar.gz", "README", "nctx-mesa-x.tar"):
                open(os.path.join(d, n), "w").close()
            try:
                config.GUEST_MESA_DIR = d
                self.assertEqual(config.guest_mesa_tarball(), os.path.join(d, "nctx-mesa-26.2.6.tar.gz"))
            finally:
                config.GUEST_MESA_DIR = old

    def test_agent_handles_native_flag(self):
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "guest-tools", "gvm-agent")
        with open(p) as f:
            s = f.read()
        self.assertIn('[ -f "$ft/native" ] && [ -n "$nt" ] && [ -n "$nos" ]', s)   # 0.2-24: Fedora 44+ only
        self.assertIn("/usr/lib64/iceland-nctx", s)
        self.assertIn('echo "$nd" > "$nc" && ldconfig', s)            # 0.2-25: the loader, not the environment
        self.assertIn('GVM_AGENT_UPDATED=1 exec /usr/local/sbin/gvm-agent "$mode"', s)


if __name__ == "__main__":
    unittest.main()
