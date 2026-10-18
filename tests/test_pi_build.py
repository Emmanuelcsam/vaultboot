import hashlib
import importlib.util
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / "build" / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


layout = load("pi_layout", "pi-layout.py")
sources = load("pi_sources", "pi-sources.py")


class PiLayoutTests(unittest.TestCase):
    def table(self):
        return {"label": "dos", "sectorsize": 512, "partitions": [
            {"start": 2048, "size": 1048576, "type": "c"},
            {"start": 1050624, "size": 5242880, "type": "83"},
        ]}

    def test_expected_firmware_then_root_layout(self):
        layout.validate_layout(self.table())

    def test_official_debian_pi_gpt_partition_numbers(self):
        table = {"label": "gpt", "sectorsize": 512, "device": "/build/disk.raw", "partitions": [
            {"node": "/build/disk.raw1", "start": 1048576, "size": 5240832,
             "type": "B921B045-1DF0-41C3-AF44-4C6F280D3FAE"},
            {"node": "/build/disk.raw15", "start": 2048, "size": 1046528,
             "type": "C12A7328-F81F-11D2-BA4B-00A0C93EC93B"},
        ]}
        boot, root = layout.validate_layout(table)
        self.assertEqual(layout.partition_number(table, boot), 15)
        self.assertEqual(layout.partition_number(table, root), 1)

    def test_unexpected_partition_is_refused(self):
        table = self.table()
        table["partitions"].append({"start": 9000000, "size": 4096, "type": "83"})
        with self.assertRaisesRegex(ValueError, "exactly two"):
            layout.validate_layout(table)

    def test_overlap_refused(self):
        table = self.table()
        table["partitions"][1]["start"] = 4096
        with self.assertRaisesRegex(ValueError, "Overlapping"):
            layout.validate_layout(table)

    def test_swapped_boot_and_root_refused(self):
        table = self.table()
        table["partitions"][0]["type"] = "83"
        with self.assertRaisesRegex(ValueError, "firmware"):
            layout.validate_layout(table)


class PiSourceIntegrityTests(unittest.TestCase):
    def fixture(self, directory, name="package.orig.tar.xz"):
        payload = b"full corresponding source fixture"
        (directory / name).write_bytes(payload)
        digest = hashlib.sha256(payload).hexdigest()
        (directory / "package.dsc").write_text(
            f"Source: package\nVersion: 1.0\nChecksums-Sha256:\n {digest} {len(payload)} {name}\n",
            encoding="utf-8",
        )

    def test_full_source_is_verified(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            self.fixture(directory)
            self.assertEqual(sources.verify_dsc_files(directory), 1)

    def test_dsc_without_source_payload_is_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            self.fixture(directory)
            (directory / "package.orig.tar.xz").unlink()
            with self.assertRaisesRegex(ValueError, "Missing or truncated"):
                sources.verify_dsc_files(directory)

    def test_same_length_corruption_is_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            self.fixture(directory)
            path = directory / "package.orig.tar.xz"
            path.write_bytes(b"x" * path.stat().st_size)
            with self.assertRaisesRegex(ValueError, "checksum mismatch"):
                sources.verify_dsc_files(directory)

    def test_empty_source_collection_is_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(ValueError, "No source control"):
                sources.verify_dsc_files(Path(temporary))


if __name__ == "__main__":
    unittest.main()
