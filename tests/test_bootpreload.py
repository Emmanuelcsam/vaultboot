"""Isolated boot-device selection and preloader lifecycle; no real disks."""

from contextlib import nullcontext
import importlib.util
import io
from pathlib import Path
import os
import stat
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("vaultboot_bootpreload", ROOT / "config/includes.chroot/usr/local/lib/vaultboot/bootpreload.py")
boot = importlib.util.module_from_spec(SPEC)
sys.path.insert(0, str(Path(SPEC.origin).parent))
try:
    SPEC.loader.exec_module(boot)
finally:
    sys.path.pop(0)


def mount(identity, path, filesystem="ext4"):
    return f"20 1 {identity} / {path} rw - {filesystem} /dev/fixture rw\n"


class BootPreloadTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.error = self.root / "offline-error.txt"
        self.addCleanup(patch.stopall)
        patch.object(boot, "ERROR", self.error).start()
        patch.object(boot.os, "geteuid", return_value=0).start()
        patch.object(boot, "architecture", return_value="amd64").start()

    def test_live_medium_wins_over_overlay_or_different_root(self):
        self.assertEqual(boot.mounted_boot_id(mount("0:42", "/", "overlay") + mount("8:1", "/run/live/medium", "iso9660")), "8:1")
        self.assertEqual(boot.mounted_boot_id(mount("8:2", "/") + mount("259:3", "/run/live/medium")), "259:3")

    def test_plain_root_selected_but_anonymous_root_never_scans_others(self):
        self.assertEqual(boot.mounted_boot_id(mount("179:2", "/")), "179:2")
        self.assertIsNone(boot.mounted_boot_id(mount("0:42", "/", "overlay") + mount("8:1", "/media/another-usb")))

    def test_ambiguous_mount_identity_is_rejected(self):
        with self.assertRaises(boot.preload.PreloadError):
            boot.mounted_boot_id(mount("8:1", "/run/live/medium") + mount("8:2", "/run/live/medium"))
        self.assertEqual(boot.mounted_boot_id(mount("8:1", "/") * 2), "8:1")

    def device_fixture(self):
        mounts = self.root / "mountinfo"
        mounts.write_text(mount("8:1", "/run/live/medium"))
        sys_devices = self.root / "sys/dev/block"
        sys_devices.mkdir(parents=True)
        disk = self.root / "sys/devices/sda"
        partition = disk / "sda1"
        partition.mkdir(parents=True)
        (partition / "dev").write_text("8:1\n")
        (partition / "partition").write_text("1\n")
        (disk / "dev").write_text("8:0\n")
        (disk / "uevent").write_text("DEVNAME=sda\nDEVTYPE=disk\n")
        (disk / "slaves").mkdir()
        (sys_devices / "8:1").symlink_to(partition)
        return mounts, sys_devices, disk

    def test_partition_resolves_to_its_one_whole_disk(self):
        mounts, sys_devices, _disk = self.device_fixture()
        self.assertEqual(boot.boot_device(mounts, sys_devices), (Path("/dev/sda"), "8:0"))

    def test_stacked_or_optical_device_is_not_guessed(self):
        mounts, sys_devices, disk = self.device_fixture()
        (disk / "slaves/other").mkdir()
        self.assertIsNone(boot.boot_device(mounts, sys_devices))
        (disk / "slaves/other").rmdir()
        (disk / "device").mkdir()
        (disk / "device/type").write_text("5\n")
        self.assertIsNone(boot.boot_device(mounts, sys_devices))

    def test_changed_identity_and_unsafe_devname_are_rejected(self):
        mounts, sys_devices, disk = self.device_fixture()
        (disk / "sda1/dev").write_text("8:99\n")
        with self.assertRaises(boot.preload.PreloadError):
            boot.boot_device(mounts, sys_devices)
        (disk / "sda1/dev").write_text("8:1\n")
        (disk / "uevent").write_text("DEVNAME=../../different\nDEVTYPE=disk\n")
        with self.assertRaises(boot.preload.PreloadError):
            boot.boot_device(mounts, sys_devices)

    def test_only_readonly_exact_block_descriptor_is_accepted(self):
        info = SimpleNamespace(st_mode=stat.S_IFBLK | 0o600, st_rdev=os.makedev(8, 0))
        stream = io.BytesIO(b"test bytes")
        with patch.object(boot.os, "open", return_value=123) as opening, patch.object(boot.os, "fstat", return_value=info), patch.object(boot.os, "fdopen", return_value=stream):
            with boot.open_boot_device(Path("/dev/sda"), "8:0") as value:
                self.assertEqual(value.read(), b"test bytes")
        self.assertEqual(opening.call_args.args[1], os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)

    def test_opened_device_change_closes_descriptor(self):
        info = SimpleNamespace(st_mode=stat.S_IFBLK | 0o600, st_rdev=os.makedev(8, 16))
        with patch.object(boot.os, "open", return_value=123), patch.object(boot.os, "fstat", return_value=info), patch.object(boot.os, "close") as close:
            with self.assertRaises(boot.preload.PreloadError):
                with boot.open_boot_device(Path("/dev/sda"), "8:0"):
                    self.fail("A different disk was opened")
        close.assert_called_once_with(123)

    def test_valid_carried_cache_avoids_any_device_access(self):
        self.error.write_text("stale error")
        with patch.object(boot.preload, "open_cache", return_value=nullcontext(object())), patch.object(boot, "boot_device") as device:
            self.assertEqual(boot.prepare(), "cached")
        device.assert_not_called()
        self.assertFalse(self.error.exists())

    def test_non_root_preparer_cannot_read_a_device_or_populate_cache(self):
        with patch.object(boot.os, "geteuid", return_value=1000), patch.object(boot.preload, "open_cache") as cache, patch.object(boot, "boot_device") as device:
            with self.assertRaisesRegex(boot.preload.PreloadError, "root"):
                boot.prepare()
        cache.assert_not_called()
        device.assert_not_called()

    def test_no_boot_device_or_slot_is_normal_unprepared_media(self):
        for device, slot in [(None, None), ((Path("/dev/sda"), "8:0"), None)]:
            with self.subTest(device=device), patch.object(boot.preload, "open_cache", return_value=nullcontext(None)), patch.object(boot, "boot_device", return_value=device), patch.object(boot, "open_boot_device", return_value=nullcontext(io.BytesIO())), patch.object(boot.preload, "locate_slot", return_value=slot), patch.object(boot.preload, "write_cache_from_slot") as copying:
                self.assertEqual(boot.prepare(), "absent")
                copying.assert_not_called()

    def test_slot_is_copied_with_shipped_architecture_pin_only(self):
        asset = {"architecture": "amd64", "sha256": "pin"}
        stream = io.BytesIO()
        for receipt, result in [(None, "absent"), ({"terms_accepted": True}, "prepared")]:
            with self.subTest(receipt=receipt), patch.object(boot.preload, "open_cache", return_value=nullcontext(None)), patch.object(boot, "boot_device", return_value=(Path("/dev/sda"), "8:0")), patch.object(boot, "open_boot_device", return_value=nullcontext(stream)), patch.object(boot.preload, "locate_slot", return_value=(4096, 1024**2)), patch.object(boot.preload, "load_manifest", return_value={"pin": True}) as manifest, patch.object(boot.preload, "asset_for", return_value=asset) as pinned, patch.object(boot.preload, "write_cache_from_slot", return_value=receipt) as copying:
                self.assertEqual(boot.prepare(), result)
                pinned.assert_called_once_with({"pin": True}, "amd64")
                manifest.assert_called_once_with()
                copying.assert_called_once_with(stream, 4096, 1024**2, asset)

    def test_bad_existing_cache_without_raw_slot_is_not_silently_ignored(self):
        with patch.object(boot.preload, "open_cache", side_effect=boot.preload.PreloadError("cache damaged")), patch.object(boot, "boot_device", return_value=None):
            with self.assertRaisesRegex(boot.preload.PreloadError, "cache damaged"):
                boot.prepare()

    def test_failure_is_reported_and_service_can_fail_without_network_or_gui(self):
        with patch.object(boot, "prepare", side_effect=boot.preload.PreloadError("slot damaged")), patch.object(boot, "report_error") as report, patch.object(boot.signal, "signal"), patch("builtins.print"):
            self.assertEqual(boot.main(), 1)
        self.assertEqual(str(report.call_args.args[0]), "slot damaged")

    def test_error_report_is_readable_bounded_and_replaces_instead_of_following_link(self):
        outside = self.root / "keep.txt"
        outside.write_text("do not change")
        self.error.symlink_to(outside)
        trusted = SimpleNamespace(st_mode=stat.S_IFDIR | 0o755, st_uid=0)
        with patch.object(boot, "STATE", self.root), patch.object(Path, "lstat", return_value=trusted):
            boot.report_error("x" * 10000)
        self.assertEqual(outside.read_text(), "do not change")
        self.assertFalse(self.error.is_symlink())
        self.assertLess(len(self.error.read_text()), 2100)
        self.assertEqual(self.error.stat().st_mode & 0o777, 0o644)
        self.assertFalse(list(self.root.glob(".offline-error-*")))


if __name__ == "__main__":
    unittest.main()
