"""Offline transport trust checks using only temporary regular files."""

from contextlib import contextmanager
import hashlib
import importlib
import io
import json
import os
from pathlib import Path
import stat
import struct
import sys
import tempfile
import unittest
from unittest import mock
import uuid
import zlib

LIB = Path(__file__).resolve().parents[1] / "config/includes.chroot/usr/local/lib/vaultboot"
sys.path.insert(0, str(LIB))
try:
    preload = importlib.import_module("preload")
finally:
    sys.path.pop(0)

PAYLOAD = b"An official package is opaque test data here.\n" * 7


def asset(arch="amd64"):
    name = "obsidian_1.14.4_amd64.deb" if arch == "amd64" else "obsidian-1.14.4-arm64.tar.gz"
    return {"version": "v1.14.4", "architecture": arch,
            "format": "deb" if arch == "amd64" else "tar", "name": name,
            "url": f"https://github.com/obsidianmd/obsidian-releases/releases/download/v1.14.4/{name}",
            "size": len(PAYLOAD), "sha256": hashlib.sha256(PAYLOAD).hexdigest()}


@contextmanager
def disk_file():
    with tempfile.TemporaryFile() as stream:
        stream.truncate(128 * 512)
        yield stream


def mbr_image(stream, kind=0xDA, start=8, size=16):
    mbr = bytearray(512)
    mbr[450] = kind
    struct.pack_into("<II", mbr, 454, start, size)
    mbr[510:] = b"\x55\xaa"
    stream.seek(0)
    stream.write(mbr)
    stream.flush()


def gpt_image(stream, overlap=False):
    """Small complete GPT, with three entries and independently checked copies."""
    mbr_image(stream, 0xEE, 1, 127)
    table = bytearray(512)
    for index, (kind, start, end) in enumerate([
            (uuid.UUID("c12a7328-f81f-11d2-ba4b-00a0c93ec93b").bytes_le, 3, 7),
            (preload.SLOT_GUID, 8, 24),
            (uuid.UUID("0fc63daf-8483-4772-8e79-3d69d8477de4").bytes_le, 24 if overlap else 25, 100)]):
        at = index * 128
        table[at:at + 16] = kind
        table[at + 16:at + 32] = uuid.UUID(int=index + 1).bytes_le
        struct.pack_into("<QQ", table, at + 32, start, end)
    for current, backup, table_lba in ((1, 127, 2), (127, 1, 126)):
        header = bytearray(512)
        header[:8] = b"EFI PART"
        struct.pack_into("<III", header, 8, 0x10000, 92, 0)
        struct.pack_into("<QQQQ", header, 24, current, backup, 3, 125)
        header[56:72] = uuid.UUID(int=42).bytes_le
        struct.pack_into("<QIII", header, 72, table_lba, 4, 128, zlib.crc32(table))
        struct.pack_into("<I", header, 16, zlib.crc32(header[:92]))
        stream.seek(table_lba * 512)
        stream.write(table)
        stream.seek(current * 512)
        stream.write(header)
    stream.flush()


class ManifestTests(unittest.TestCase):
    def test_shipped_manifest_pins_both_official_architectures(self):
        # LIB is usr/local/lib/vaultboot; the share directory is under usr.
        path = LIB.parents[2] / "share/vaultboot/obsidian-offline.json"
        pins = preload.load_manifest(path)
        self.assertEqual(set(pins["assets"]), {"amd64", "arm64"})
        for arch in pins["assets"]:
            value = preload.asset_for(pins, arch)
            self.assertEqual(value["architecture"], arch)
            self.assertGreater(value["size"], 100 * 1024 * 1024)
            self.assertEqual(len(value["sha256"]), 64)

    def test_invalid_pin_fields_cannot_authorize_an_arbitrary_asset(self):
        for change in ({"architecture": "arm64"}, {"format": "tar"}, {"size": True},
                       {"name": "../package.deb"}, {"url": "https://example.test/app.deb"},
                       {"sha256": "0" * 63}, {"version": "latest"}):
            with self.subTest(change=change):
                value = asset() | change
                with self.assertRaises(preload.PreloadError):
                    preload.asset_for({"assets": {"amd64": value}}, "amd64")

    def test_duplicate_or_oversized_manifest_rejected(self):
        for text in ('{"schema":1,"schema":1,"assets":{}}', " " * 65537):
            with self.subTest(length=len(text)), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "pins.json"
                path.write_text(text)
                with self.assertRaises(preload.PreloadError):
                    preload.load_manifest(path)


class SlotTests(unittest.TestCase):
    def test_empty_mbr_slot_then_verified_round_trip(self):
        with disk_file() as stream:
            mbr_image(stream)
            slot = preload.locate_slot(stream)
            self.assertEqual(slot, (4096, 8192))
            self.assertIsNone(preload.read_slot(stream, *slot, asset()))
            preload.write_slot(stream, *slot, asset(), io.BytesIO(PAYLOAD))
            self.assertEqual(preload.read_slot(stream, *slot, asset()), preload.receipt_for(asset()))
            with self.assertRaisesRegex(preload.PreloadError, "already prepared"):
                preload.write_slot(stream, *slot, asset(), io.BytesIO(PAYLOAD))

    def test_wrong_download_never_publishes_ready_header(self):
        for payload in (b"!" + PAYLOAD[1:], PAYLOAD[:-1], PAYLOAD + b"extra"):
            with self.subTest(length=len(payload)), disk_file() as stream:
                mbr_image(stream)
                slot = preload.locate_slot(stream)
                with self.assertRaises(preload.PreloadError):
                    preload.write_slot(stream, *slot, asset(), io.BytesIO(payload))
                stream.seek(slot[0])
                self.assertEqual(stream.read(preload.HEADER_SIZE), bytes(preload.HEADER_SIZE))

    def test_corrupt_header_padding_and_payload_are_rejected(self):
        for position in (0, 16, preload.HEADER_SIZE - 1, preload.HEADER_SIZE + 3):
            with self.subTest(position=position), disk_file() as stream:
                mbr_image(stream)
                slot = preload.locate_slot(stream)
                preload.write_slot(stream, *slot, asset(), io.BytesIO(PAYLOAD))
                stream.seek(slot[0] + position)
                original = stream.read(1)
                stream.seek(slot[0] + position)
                stream.write(bytes([original[0] ^ 255]))
                with self.assertRaises(preload.PreloadError):
                    preload.read_slot(stream, *slot, asset())

    def test_wrong_architecture_and_unaccepted_receipts_are_rejected(self):
        for receipt in (preload.receipt_for(asset("arm64")),
                        preload.receipt_for(asset()) | {"terms_accepted": False}):
            with self.subTest(receipt=receipt), disk_file() as stream:
                body = json.dumps(receipt).encode()
                stream.seek(4096)
                stream.write(preload.MAGIC + struct.pack("<I", len(body)) + body +
                             bytes(preload.HEADER_SIZE - 20 - len(body)))
                stream.write(PAYLOAD)
                with self.assertRaisesRegex(preload.PreloadError, "receipt"):
                    preload.read_slot(stream, 4096, 8192, asset())

    def test_package_and_slot_bounds_are_checked(self):
        with disk_file() as stream:
            with self.assertRaisesRegex(preload.PreloadError, "bounds"):
                preload.read_slot(stream, 65000, 8192, asset())
            large = asset() | {"size": 8193}
            with self.assertRaisesRegex(preload.PreloadError, "does not fit"):
                preload.write_slot(stream, 4096, 8192, large, io.BytesIO(PAYLOAD))

    def test_gpt_locates_reserved_type_and_checks_both_copies(self):
        with disk_file() as stream:
            gpt_image(stream)
            self.assertEqual(preload.locate_slot(stream), (4096, 17 * 512))
            stream.seek(127 * 512 + 56)
            stream.write(b"!")
            with self.assertRaisesRegex(preload.PreloadError, "header checksum"):
                preload.locate_slot(stream)

    def test_gpt_table_crc_and_partition_overlap_are_rejected(self):
        with disk_file() as stream:
            gpt_image(stream)
            stream.seek(2 * 512 + 16)
            stream.write(b"!")
            with self.assertRaisesRegex(preload.PreloadError, "table checksum"):
                preload.locate_slot(stream)
        with disk_file() as stream:
            gpt_image(stream, overlap=True)
            with self.assertRaisesRegex(preload.PreloadError, "overlaps"):
                preload.locate_slot(stream)

    def test_individually_valid_but_disagreeing_gpt_copies_are_rejected(self):
        with disk_file() as stream:
            gpt_image(stream)
            stream.seek(127 * 512)
            header = bytearray(stream.read(512))
            header[56:72] = uuid.UUID(int=43).bytes_le
            struct.pack_into("<I", header, 16, 0)
            struct.pack_into("<I", header, 16, zlib.crc32(header[:92]))
            stream.seek(127 * 512)
            stream.write(header)
            with self.assertRaisesRegex(preload.PreloadError, "copies disagree"):
                preload.locate_slot(stream)

    def test_legacy_no_slot_is_absent_and_protective_mbr_without_gpt_is_corrupt(self):
        with disk_file() as stream:
            mbr_image(stream, kind=0x83)
            self.assertIsNone(preload.locate_slot(stream))
            mbr_image(stream, kind=0xEE)
            with self.assertRaisesRegex(preload.PreloadError, "no valid GPT"):
                preload.locate_slot(stream)


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.cache = self.directory / "cache"
        self.cache.mkdir(mode=0o755)
        self.manifest = self.directory / "pins.json"
        self.manifest.write_text(json.dumps({"schema": 1, "assets": {"amd64": asset(), "arm64": asset("arm64")}}))
        patch = mock.patch.object(preload, "TRUSTED_UID", os.getuid())
        patch.start()
        self.addCleanup(patch.stop)

    def prepare(self):
        package = self.cache / asset()["name"]
        package.write_bytes(PAYLOAD)
        package.chmod(0o644)
        receipt = self.cache / "ready.json"
        receipt.write_text(json.dumps(preload.receipt_for(asset())))
        receipt.chmod(0o644)
        return package

    def opened(self, arch="amd64"):
        return preload.open_cache(arch, self.manifest, self.cache)

    def test_missing_and_empty_cache_are_absent(self):
        with self.opened() as cached:
            self.assertIsNone(cached)
        self.cache.rmdir()
        with self.opened() as cached:
            self.assertIsNone(cached)

    def test_valid_cache_is_verified_and_descriptor_retained_until_exit(self):
        self.prepare()
        with self.opened() as cached:
            self.assertEqual(cached.asset, asset())
            self.assertTrue(cached.receipt["terms_accepted"])
            self.assertEqual(cached.stream.tell(), 0)
            self.assertEqual(cached.stream.read(), PAYLOAD)
            self.assertFalse(cached.stream.closed)
        self.assertTrue(cached.stream.closed)

    def test_partial_wrong_size_corrupt_and_wrong_architecture_caches_rejected(self):
        package = self.prepare()
        for payload in (PAYLOAD[:-1], PAYLOAD + b"!", b"!" + PAYLOAD[1:]):
            with self.subTest(length=len(payload)):
                package.write_bytes(payload)
                with self.assertRaisesRegex(preload.PreloadError, "checksum"):
                    with self.opened():
                        pass
        package.write_bytes(PAYLOAD)
        with self.assertRaisesRegex(preload.PreloadError, "receipt"):
            with self.opened("arm64"):
                pass
        (self.cache / "ready.json").unlink()
        with self.assertRaises(preload.PreloadError):
            with self.opened():
                pass

    def test_symlink_file_and_directory_rejected(self):
        package = self.prepare()
        other = self.directory / "actual-package"
        package.rename(other)
        package.symlink_to(other)
        with self.assertRaises(preload.PreloadError):
            with self.opened():
                pass
        actual = self.directory / "actual-cache"
        self.cache.rename(actual)
        self.cache.symlink_to(actual, target_is_directory=True)
        with self.assertRaises(preload.PreloadError):
            with self.opened():
                pass

    def test_writable_file_leaf_and_ancestor_rejected(self):
        package = self.prepare()
        for path in (package, self.cache / "ready.json", self.cache, self.directory):
            with self.subTest(path=path.name):
                original = stat.S_IMODE(path.stat().st_mode)
                path.chmod(original | 0o020)
                try:
                    with self.assertRaises(preload.PreloadError):
                        with self.opened():
                            pass
                finally:
                    path.chmod(original)

    def test_other_owner_and_unaccepted_receipt_are_rejected(self):
        self.prepare()
        with mock.patch.object(preload, "TRUSTED_UID", os.getuid() + 1):
            with self.assertRaises(preload.PreloadError):
                with self.opened():
                    pass
        (self.cache / "ready.json").write_text(json.dumps(
            preload.receipt_for(asset()) | {"terms_accepted": False}))
        with self.assertRaisesRegex(preload.PreloadError, "accepted terms"):
            with self.opened():
                pass

    def test_fifo_package_is_rejected_without_blocking(self):
        package = self.prepare()
        package.unlink()
        os.mkfifo(package, 0o600)
        with self.assertRaisesRegex(preload.PreloadError, "not protected"):
            with self.opened():
                pass

    def test_copy_reads_held_descriptor_even_if_path_is_replaced(self):
        package = self.prepare()
        destination = self.directory / "fresh/var/lib/vaultboot/offline"
        with self.opened() as cached:
            package.rename(self.cache / "original-package")
            package.write_bytes(b"attacker replacement")
            preload.copy_cached(cached, destination)
        self.assertEqual((destination / asset()["name"]).read_bytes(), PAYLOAD)
        self.assertEqual(json.loads((destination / "ready.json").read_text()), preload.receipt_for(asset()))
        self.assertEqual({path.name for path in destination.iterdir()}, {asset()["name"], "ready.json"})
        with preload.open_cache("amd64", self.manifest, destination) as cached:
            self.assertEqual(cached.stream.read(), PAYLOAD)

    def test_in_place_change_rechecked_during_copy_and_receipt_not_published(self):
        package = self.prepare()
        destination = self.directory / "fresh-cache"
        with self.opened() as cached:
            package.write_bytes(b"!" + PAYLOAD[1:])
            with self.assertRaisesRegex(preload.PreloadError, "changed while copying"):
                preload.copy_cached(cached, destination)
        self.assertFalse((destination / "ready.json").exists())
        self.assertEqual(list(destination.iterdir()), [])

    def test_copy_refuses_symlink_destination_without_writing_elsewhere(self):
        self.prepare()
        actual = self.directory / "elsewhere"
        actual.mkdir(mode=0o755)
        destination = self.directory / "symlink"
        destination.symlink_to(actual, target_is_directory=True)
        with self.opened() as cached:
            with self.assertRaises(preload.PreloadError):
                preload.copy_cached(cached, destination / "child")
        self.assertEqual(list(actual.iterdir()), [])

    def test_slot_to_cache_round_trip_verifies_package_again(self):
        with disk_file() as stream:
            mbr_image(stream)
            slot = preload.locate_slot(stream)
            preload.write_slot(stream, *slot, asset(), io.BytesIO(PAYLOAD))
            self.assertEqual(preload.write_cache_from_slot(stream, *slot, asset(), self.cache),
                             preload.receipt_for(asset()))
        with self.opened() as cached:
            self.assertEqual(cached.stream.read(), PAYLOAD)


if __name__ == "__main__":
    unittest.main()
