"""Disk-image safety and layout checks; all writes stay in TemporaryDirectory."""

import importlib.util
import json
import os
from pathlib import Path
import shutil
import stat
import struct
import tempfile
import unittest
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "make-usb-image.py"
spec = importlib.util.spec_from_file_location("usb_image", SCRIPT)
image = importlib.util.module_from_spec(spec)
spec.loader.exec_module(image)
ISOLINUX = Path(os.environ.get("VAULTBOOT_ISOLINUX_DIR", "/usr/lib/ISOLINUX"))


class ImageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.iso = self.root / "input.iso"
        self.iso.write_bytes(b"unmodified source")
        self.out = self.root / "output.img"

    def test_input_symlink_rejected(self):
        link = self.root / "link.iso"
        link.symlink_to(self.iso)
        with self.assertRaisesRegex(ValueError, "regular ISO"):
            image.make_image(link, self.out, 64)

    def test_input_device_rejected(self):
        with self.assertRaisesRegex(ValueError, "regular ISO"):
            image.make_image(Path("/dev/null"), self.out, 64)

    def test_block_device_rejected_before_running_commands(self):
        with mock.patch.object(Path, "lstat", return_value=mock.Mock(st_mode=stat.S_IFBLK)):
            with self.assertRaisesRegex(ValueError, "regular ISO"):
                image.make_image(self.iso, self.out, 64)

    def test_existing_output_rejected_and_preserved(self):
        self.out.write_bytes(b"existing data")
        with self.assertRaisesRegex(ValueError, "existing path"):
            image.make_image(self.iso, self.out, 64)
        self.assertEqual(self.out.read_bytes(), b"existing data")

    def test_output_symlink_rejected_and_target_preserved(self):
        self.out.symlink_to(self.iso)
        with self.assertRaisesRegex(ValueError, "existing path"):
            image.make_image(self.iso, self.out, 64)
        self.assertEqual(self.iso.read_bytes(), b"unmodified source")

    def test_dangling_output_symlink_rejected(self):
        self.out.symlink_to(self.root / "missing")
        with self.assertRaisesRegex(ValueError, "existing path"):
            image.make_image(self.iso, self.out, 64)

    def test_invalid_mbr_rejected(self):
        with self.assertRaisesRegex(ValueError, "hybrid MBR"):
            image.partitions(self.iso)

    def fixture(self, *, overlap=False, label=b"vaultboot-data", length=64 * 1024 * 1024):
        header = bytearray(512)
        header[510:512] = b"\x55\xaa"
        for number, kind, start, size in ((1, 0, 0, 4097 if overlap else 4096),
                                         (2, 0xEF, 1024, 2048), (3, 0x83, 4096, length // 512)):
            at = 446 + (number - 1) * 16
            header[at + 4] = kind
            struct.pack_into("<II", header, at + 8, start, size)
        with self.out.open("wb") as out:
            out.write(header)
            out.seek(4096 * 512 + 1024)
            superblock = bytearray(1024)
            superblock[56:58] = b"\x53\xef"
            superblock[120:120 + len(label)] = label
            out.write(superblock)
            out.truncate(4096 * 512 + length)
        return length

    def test_ext4_layout_accepts_existing_iso_efi_nesting(self):
        size = self.fixture()
        self.assertEqual(image.check_output(self.out, size)[2]["number"], 3)

    def test_persistence_overlap_rejected(self):
        size = self.fixture(overlap=True)
        with self.assertRaisesRegex(ValueError, "overlaps"):
            image.check_output(self.out, size)

    def test_incorrect_persistence_label_rejected(self):
        size = self.fixture(label=b"wrong")
        with self.assertRaisesRegex(ValueError, "labeled ext4"):
            image.check_output(self.out, size)

    def test_truncated_image_rejected(self):
        size = self.fixture()
        with self.out.open("r+b") as stream:
            stream.truncate(3 * 1024 * 1024)
        with self.assertRaisesRegex(ValueError, "past the image"):
            image.check_output(self.out, size)

    def test_missing_boot_platform_rejected(self):
        with mock.patch.object(image, "run", return_value="El Torito boot img : 1 BIOS"):
            with self.assertRaisesRegex(ValueError, "UEFI"):
                image.boot_report(self.iso)


class HybridIntegrationTests(unittest.TestCase):
    @unittest.skipUnless((ISOLINUX / "isolinux.bin").exists() and shutil.which("xorriso"),
                         "install isolinux, xorriso and e2fsprogs for the real layout test")
    def test_real_xorriso_persistence_preserves_input_and_both_boot_entries(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tree = root / "tree"
            tree.mkdir()
            shutil.copyfile(ISOLINUX / "isolinux.bin", tree / "isolinux.bin")
            # This dummy EFI image tests partition layout, not firmware execution.
            with (tree / "efi.img").open("wb") as stream:
                stream.truncate(1024 * 1024)
            iso, output = root / "input.iso", root / "output.img"
            image.run("xorriso", "-as", "mkisofs", "-o", str(iso),
                      "-isohybrid-mbr", str(ISOLINUX / "isohdpfx.bin"),
                      "-b", "isolinux.bin", "-c", "boot.cat", "-no-emul-boot",
                      "-boot-load-size", "4", "-boot-info-table", "-eltorito-alt-boot",
                      "-e", "efi.img", "-no-emul-boot", "-isohybrid-gpt-basdat",
                      "-partition_offset", "16", str(tree))
            original = iso.read_bytes()
            image.make_image(iso, output, 64)
            self.assertEqual(iso.read_bytes(), original)
            layout = json.loads(output.with_name(output.name + ".layout.json").read_text())
            self.assertEqual(layout["persistence_bytes"], 64 * 1024 * 1024)
            self.assertIn("BIOS", layout["boot_report"])
            self.assertIn("UEFI", layout["boot_report"])
            self.assertEqual(layout["mbr_partitions"][-1]["type"], 0xDA)
            self.assertEqual(layout["offline_size_bytes"], 256 * 1024 * 1024)
            self.assertGreater(layout["offline_offset_bytes"], layout["persistence_bytes"])
            self.assertEqual(layout["offline_name"], "VAULTBOOT-OFFLINE")


if __name__ == "__main__":
    unittest.main()
