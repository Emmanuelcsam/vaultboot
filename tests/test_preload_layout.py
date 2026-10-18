"""Synthetic regular-file GPT tests; no disk devices or mounts are used."""

import importlib.util
import os
from pathlib import Path
import struct
import sys
import tempfile
import unittest
import uuid
import zlib

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


layout = load("preload_layout", ROOT / "build/preload-layout.py")
transport = load("preload_layout_transport", ROOT / "config/includes.chroot/usr/local/lib/vaultboot/preload.py")


class PreloadLayoutTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "disk.img"

    def fixture(self, pi=False):
        """Create independent, CRC-correct GPTs with deliberate PC EFI nesting."""
        mib = 1024 * 1024
        size = 8 * mib if pi else 265 * mib
        sectors = size // 512
        first = 2048 if pi else 64
        table = bytearray(128 * 128)
        generic = uuid.UUID("ebd0a0a2-b9e5-4433-87c0-68b6b72699c7")
        parts = ((1, layout.ARM_ROOT_GUID, 4096, sectors - 34),
                 (15, layout.EFI_GUID, 2048, 4095)) if pi else (
                     (1, generic, 64, 8191), (2, layout.EFI_GUID, 1024, 3071),
                     (3, generic, 8192, 16383), (4, generic, 16384, 16384 + layout.SLOT_SIZE // 512 - 1))
        for number, kind, start, end in parts:
            at = (number - 1) * 128
            table[at:at + 16] = kind.bytes_le
            table[at + 16:at + 32] = uuid.uuid5(uuid.NAMESPACE_DNS, f"fixture-{number}").bytes_le
            struct.pack_into("<QQQ", table, at + 32, start, end, 0)
        mbr = bytearray(512)
        mbr[:16] = b"bootcode-fixture"
        mbr[510:512] = b"\x55\xaa"
        mbr_parts = ((1, 0x0C, 2048, 2048), (2, 0xEE, 1, 2047), (3, 0xEE, 4096, 4096)) if pi else (
            (1, 0, 64, 8128), (2, 0xEF, 1024, 2048), (3, 0x83, 8192, 8192),
            (4, 0xDA, 16384, layout.SLOT_SIZE // 512))
        for number, kind, start, count in mbr_parts:
            at = 446 + (number - 1) * 16
            mbr[at + 4] = kind
            struct.pack_into("<II", mbr, at + 8, start, count)
        disk_id = uuid.UUID("d431165b-519b-4fdd-9b1f-3498c43009c2")
        with self.path.open("wb") as stream:
            stream.truncate(size)
            stream.write(mbr)
            for own, other, table_lba in ((1, sectors - 1, 2), (sectors - 1, 1, sectors - 33)):
                header = bytearray(512)
                header[:8] = b"EFI PART"
                struct.pack_into("<IIIIQQQQ", header, 8, 0x10000, 92, 0, 0,
                                 own, other, first, sectors - 34)
                header[56:72] = disk_id.bytes_le
                struct.pack_into("<QIII", header, 72, table_lba, 128, 128, zlib.crc32(table))
                struct.pack_into("<I", header, 16, zlib.crc32(header[:92]))
                stream.seek(table_lba * 512)
                stream.write(table)
                stream.seek(own * 512)
                stream.write(header)
            for start in (2048, 4096):
                stream.seek(start * 512)
                stream.write(b"preserved-partition-data")
        return size, bytes(mbr), bytes(table)

    def inspect(self):
        with layout.open_image(self.path) as stream:
            return layout.read_gpt(stream)

    def edit_table(self, edit):
        """Keep CRCs valid while injecting a structurally bad partition table."""
        with layout.open_image(self.path, True) as stream:
            gpt = layout.read_gpt(stream)
            table = bytearray(gpt["primary"]["table"])
            edit(table)
            layout.write_tables(stream, layout.updated_header(gpt["primary"], table),
                                layout.updated_header(gpt["backup"], table), table)

    def test_pc_slot_type_and_name_preserve_both_boot_records(self):
        size, mbr, original_table = self.fixture()
        before = self.inspect()
        result = layout.prepare_pc(self.path)
        after = self.inspect()
        self.assertEqual(after["mbr"], mbr)
        self.assertEqual(after["size"], size)
        self.assertEqual(after["primary"]["table"][:384], original_table[:384])
        self.assertEqual(after["primary"]["disk_guid"], before["primary"]["disk_guid"])
        self.assertEqual(after["primary"]["table"][400:440], original_table[400:440])
        self.assertEqual(result["offline_partition"], 4)
        self.assertEqual(result["offline_size_bytes"], 256 * 1024 * 1024)
        with self.path.open("rb") as stream:
            self.assertEqual(transport.locate_slot(stream), (result["offline_offset_bytes"], result["offline_size_bytes"]))

    def test_pi_slot_grows_image_preserving_firmware_root_and_hybrid_mbr(self):
        size, mbr, original_table = self.fixture(pi=True)
        before = self.inspect()
        result = layout.append_pi(self.path)
        after = self.inspect()
        self.assertEqual(after["mbr"], mbr)
        self.assertEqual(after["size"], size + 257 * 1024 * 1024)
        self.assertEqual(result["offline_offset_bytes"], size)
        self.assertEqual(result["offline_partition"], 2)
        self.assertEqual(after["primary"]["table"][:128], original_table[:128])
        self.assertEqual(after["primary"]["table"][256:], original_table[256:])
        self.assertEqual(after["primary"]["alternate"], after["size"] // 512 - 1)
        with self.path.open("rb") as stream:
            for start in (2048, 4096):
                stream.seek(start * 512)
                self.assertEqual(stream.read(len(b"preserved-partition-data")), b"preserved-partition-data")
            stream.seek(before["backup"]["table_lba"] * 512)
            self.assertEqual(stream.read(33 * 512), bytes(33 * 512))
            self.assertEqual(transport.locate_slot(stream), (size, 256 * 1024 * 1024))
            # A larger physical USB/SD retains the GPT backup at image end;
            # consumer discovery must use its pointer, not media capacity.
        with self.path.open("r+b") as stream:
            stream.truncate(after["size"] + 1024 * 1024)
            self.assertEqual(transport.locate_slot(stream), (size, 256 * 1024 * 1024))

    def test_symlink_is_rejected(self):
        self.fixture(pi=True)
        alias = self.path.with_name("link.img")
        alias.symlink_to(self.path)
        with self.assertRaisesRegex(ValueError, "regular file"):
            layout.append_pi(alias)

    def test_device_is_rejected_without_opening(self):
        with self.assertRaisesRegex(ValueError, "regular file"):
            layout.prepare_pc(Path("/dev/null"))

    def test_hard_link_is_rejected(self):
        self.fixture(pi=True)
        os.link(self.path, self.path.with_name("linked.img"))
        with self.assertRaisesRegex(ValueError, "one link"):
            layout.append_pi(self.path)

    def test_readonly_image_is_rejected(self):
        self.fixture(pi=True)
        self.path.chmod(0o444)
        with self.assertRaisesRegex(ValueError, "writable permissions"):
            layout.append_pi(self.path)

    def test_header_crc_is_checked_before_mutation(self):
        self.fixture(pi=True)
        with self.path.open("r+b") as stream:
            stream.seek(512 + 56)
            stream.write(b"wrong")
        before = self.path.stat().st_size
        with self.assertRaisesRegex(ValueError, "header CRC"):
            layout.append_pi(self.path)
        self.assertEqual(self.path.stat().st_size, before)

    def test_backup_header_crc_is_checked(self):
        self.fixture(pi=True)
        with self.path.open("r+b") as stream:
            stream.seek(-512 + 56, os.SEEK_END)
            stream.write(b"wrong")
        with self.assertRaisesRegex(ValueError, "header CRC"):
            layout.append_pi(self.path)

    def test_partition_table_crc_is_checked(self):
        self.fixture(pi=True)
        with self.path.open("r+b") as stream:
            stream.seek(1024 + 56)
            stream.write(b"wrong")
        with self.assertRaisesRegex(ValueError, "table CRC"):
            layout.append_pi(self.path)

    def test_different_but_crc_valid_gpt_copies_are_rejected(self):
        self.fixture(pi=True)
        with layout.open_image(self.path, True) as stream:
            gpt = layout.read_gpt(stream)
            table = bytearray(gpt["backup"]["table"])
            table[56] = 65
            backup = layout.updated_header(gpt["backup"], table)
            layout.write_at(stream, gpt["backup"]["table_lba"] * 512, table)
            layout.write_at(stream, gpt["backup"]["own"] * 512, backup)
        with self.assertRaisesRegex(ValueError, "contents disagree"):
            layout.append_pi(self.path)

    def test_pc_nonzero_slot_is_rejected_without_header_change(self):
        self.fixture()
        before = self.inspect()["primary"]["raw"]
        with self.path.open("r+b") as stream:
            stream.seek(8 * 1024 * 1024 + layout.SLOT_SIZE - 1)
            stream.write(b"!")
        with self.assertRaisesRegex(ValueError, "not empty"):
            layout.prepare_pc(self.path)
        self.assertEqual(self.inspect()["primary"]["raw"], before)

    def test_pc_slot_overlapping_gpt_partition_is_rejected(self):
        self.fixture()
        self.edit_table(lambda table: struct.pack_into("<Q", table, 256 + 40, 17000))
        with self.assertRaisesRegex(ValueError, "overlaps"):
            layout.prepare_pc(self.path)

    def test_pc_slot_overlapping_only_mbr_partition_is_rejected(self):
        self.fixture()
        with self.path.open("r+b") as stream:
            stream.seek(446 + 2 * 16 + 12)
            stream.write(struct.pack("<I", 9000))
        with self.assertRaisesRegex(ValueError, "overlaps an MBR"):
            layout.prepare_pc(self.path)

    def test_pc_wrong_mbr_type_is_rejected(self):
        self.fixture()
        with self.path.open("r+b") as stream:
            stream.seek(498)
            stream.write(b"\x83")
        with self.assertRaisesRegex(ValueError, "type 0xDA"):
            layout.prepare_pc(self.path)

    def test_pc_zero_transport_padding_is_trimmed_after_validation(self):
        size, mbr, _ = self.fixture()
        with self.path.open("r+b") as stream:
            stream.truncate(size + 352 * 1024)
        layout.prepare_pc(self.path)
        self.assertEqual(self.path.stat().st_size, size)
        self.assertEqual(self.inspect()["mbr"], mbr)

    def test_pc_nonzero_transport_padding_is_refused_without_trimming(self):
        size, _, _ = self.fixture()
        with self.path.open("ab") as stream:
            stream.write(b"!" + bytes(511))
        with self.assertRaisesRegex(ValueError, "not empty"):
            layout.prepare_pc(self.path)
        self.assertEqual(self.path.stat().st_size, size + 512)

    def test_pc_duplicate_offline_type_is_rejected(self):
        self.fixture()
        self.edit_table(lambda table: table.__setitem__(slice(256, 272), layout.SLOT_GUID.bytes_le))
        with self.assertRaisesRegex(ValueError, "already contains"):
            layout.prepare_pc(self.path)

    def test_pi_wrong_firmware_mbr_is_rejected(self):
        self.fixture(pi=True)
        with self.path.open("r+b") as stream:
            stream.seek(450)
            stream.write(b"\xee")
        with self.assertRaisesRegex(ValueError, "hybrid MBR"):
            layout.append_pi(self.path)

    def test_pi_repeat_append_is_rejected(self):
        self.fixture(pi=True)
        layout.append_pi(self.path)
        before = self.path.stat().st_size
        with self.assertRaisesRegex(ValueError, "root 1 and firmware 15 only"):
            layout.append_pi(self.path)
        self.assertEqual(self.path.stat().st_size, before)

    def test_out_of_bounds_partition_is_rejected(self):
        self.fixture(pi=True)
        self.edit_table(lambda table: struct.pack_into("<Q", table, 40, 999999999))
        with self.assertRaisesRegex(ValueError, "outside the usable range"):
            layout.append_pi(self.path)

    def test_truncated_image_is_rejected(self):
        self.fixture(pi=True)
        with self.path.open("r+b") as stream:
            stream.truncate(1024 * 1024)
        with self.assertRaisesRegex(ValueError, "Invalid image size"):
            layout.append_pi(self.path)


if __name__ == "__main__":
    unittest.main()
