"""Check emulator isolation and candidate identity without starting a VM."""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


VM_DIR = Path(__file__).parent / "vm"
spec = importlib.util.spec_from_file_location("console", VM_DIR / "console.py")
console = importlib.util.module_from_spec(spec)
spec.loader.exec_module(console)
spec = importlib.util.spec_from_file_location("arm_console", VM_DIR / "arm-console.py")
arm = importlib.util.module_from_spec(spec)
with patch.dict(sys.modules, {"console": console}):
    spec.loader.exec_module(arm)


class ARMConsoleTests(unittest.TestCase):
    def inputs(self, directory):
        root = Path(directory)
        paths = [root / name for name in ("candidate.img", "vmlinuz", "initrd.img")]
        for path in paths:
            path.write_bytes(b"fixture")
        return paths

    def test_candidate_is_read_only_backing_and_only_overlay_is_attached(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = self.inputs(directory)
            work = Path(directory) / "test"
            with patch.object(arm.subprocess, "run") as create, patch.object(arm.ARMVM, "start_process") as start:
                arm.ARMVM(*paths, work)
            creation = create.call_args.args[0]
            self.assertEqual(creation[creation.index("-b") + 1], str(paths[0]))
            args = start.call_args.args[0]
            drive = args[args.index("-drive") + 1]
            self.assertIn(str(work / "disk.qcow2"), drive)
            self.assertNotIn(str(paths[0]), drive)
            self.assertEqual(args[args.index("-kernel") + 1], str(paths[1]))
            self.assertEqual(args[args.index("-initrd") + 1], str(paths[2]))
            self.assertIn("root=/dev/vda1", args[args.index("-append") + 1])
            self.assertNotIn("-enable-kvm", args)
            self.assertEqual(args[args.index("-m") + 1], "1024")
            self.assertIn("virtio-net-pci,netdev=net0,id=nic0,romfile=", args)
            self.assertFalse(any("hostfwd" in value or "tcp:" in value for value in args))

    def test_reusing_an_overlay_for_a_different_candidate_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = self.inputs(directory)
            work = Path(directory) / "test"
            work.mkdir()
            (work / "disk.qcow2").write_bytes(b"fixture")
            metadata = json.dumps({"format": "qcow2", "full-backing-filename": "/different/image.img"})
            with patch.object(arm.subprocess, "check_output", return_value=metadata), patch.object(arm.ARMVM, "start_process") as start:
                with self.assertRaisesRegex(ValueError, "different image"):
                    arm.ARMVM(*paths, work)
                start.assert_not_called()

    def test_offline_mode_has_no_user_network_backend(self):
        args = arm.qemu_arguments("overlay", "kernel", "initrd", "serial", "qmp", offline=True)
        self.assertNotIn("-netdev", args)
        self.assertEqual(args[-2:], ["-nic", "none"])

    def test_extra_memory_is_an_explicit_diagnostic_override(self):
        args = arm.qemu_arguments("overlay", "kernel", "initrd", "serial", "qmp", memory=2048)
        self.assertEqual(args[args.index("-m") + 1], "2048")
        with self.assertRaisesRegex(ValueError, "memory"):
            arm.qemu_arguments("overlay", "kernel", "initrd", "serial", "qmp", memory=512)


if __name__ == "__main__":
    unittest.main()
