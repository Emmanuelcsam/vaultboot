"""AUTOFLASH safety tests: no physical devices, privilege, or network requests.

The only writable destination is a regular file inside TemporaryDirectory,
opened by FakeDisks below. Production adapters reject that file.
"""
import argparse
from dataclasses import replace
import gzip
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "autoflash.py"
spec = importlib.util.spec_from_file_location("autoflash", SCRIPT)
flash = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = flash
spec.loader.exec_module(flash)


class FakeDisks:
    """Test-only adapter; never supplied by the command-line program."""
    def __init__(self, directory, raw_size):
        self.path = directory / "fake-device"
        self.path.write_bytes(b"X" * (raw_size + 2 * flash.MIB))
        self.disk = flash.Disk("/dev/test-only", self.path.stat().st_size, "Fake USB", "fixture",
                               ("/dev/test-only", 123, 42, 99, "1"), ("/media/test",))
        self.opens = self.unmounts = self.flushes = self.ejects = self.scans = 0
        self.busy = self.remount = self.corrupt = self.flush_failure = False
        self.change_at = None

    def inventory(self):
        self.scans += 1
        if self.change_at == self.scans:
            self.disk = replace(self.disk, identity=self.disk.identity + ("changed",))
        return [self.disk]

    def unmount(self, disk):
        self.unmounts += 1
        if self.busy:
            raise flash.Refused("device busy")
        if not self.remount:
            self.disk = replace(self.disk, mounts=())

    def open(self, disk):
        assert self.path.parent.is_dir() and stat.S_ISREG(self.path.lstat().st_mode)
        self.opens += 1
        return os.open(self.path, os.O_RDWR | os.O_NOFOLLOW)

    def flush(self, fd):
        self.flushes += 1
        if self.flush_failure:
            raise flash.Refused("unsupported cache flush")
        os.fsync(fd)
        if self.corrupt and self.flushes == 2:
            os.pwrite(fd, b"bad", 512)

    def eject(self, disk):
        self.ejects += 1


class AutoflashTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        data = bytearray(b"image" * (flash.MIB // 5 + 1))[:flash.MIB]
        data[510:512] = b"\x55\xaa"
        self.data = bytes(data)
        self.raw = self.root / "input.img"
        self.raw.write_bytes(data)
        self.sha = hashlib.sha256(data).hexdigest()
        self.adapter = FakeDisks(self.root, len(data))

    def execute(self, confirmation=None):
        disk = self.adapter.disk
        flash.flash(self.adapter, disk, self.raw, len(self.data), self.sha,
                    disk.confirmation() if confirmation is None else confirmation)

    def test_complete_file_backed_write_and_readback(self):
        self.execute()
        result = self.adapter.path.read_bytes()
        self.assertEqual(result[:len(self.data)], self.data)
        self.assertEqual(result[len(self.data):-flash.MIB], b"X" * flash.MIB)
        self.assertEqual(result[-flash.MIB:], b"\0" * flash.MIB)
        self.assertEqual(self.adapter.ejects, 1)
        self.assertEqual(self.adapter.flushes, 3)

    def test_wrong_confirmation_has_no_unmount_or_write(self):
        before = self.adapter.path.read_bytes()
        with self.assertRaisesRegex(flash.Refused, "Confirmation"):
            self.execute("yes")
        self.assertEqual(self.adapter.path.read_bytes(), before)
        self.assertEqual(self.adapter.unmounts, 0)
        self.assertEqual(self.adapter.opens, 0)

    def test_busy_unmount_aborts_before_open(self):
        self.adapter.busy = True
        with self.assertRaisesRegex(flash.Refused, "busy"):
            self.execute()
        self.assertEqual(self.adapter.opens, 0)

    def test_automatic_remount_aborts_before_open(self):
        self.adapter.remount = True
        with self.assertRaisesRegex(flash.Refused, "still mounted"):
            self.execute()
        self.assertEqual(self.adapter.opens, 0)

    def test_identity_change_at_every_recheck_aborts_without_writes(self):
        for scan in (1, 2, 3):
            with self.subTest(scan=scan):
                self.adapter = FakeDisks(self.root, len(self.data))
                self.adapter.change_at = scan
                before = self.adapter.path.read_bytes()
                with self.assertRaisesRegex(flash.Refused, "identity changed"):
                    self.execute()
                self.assertEqual(self.adapter.path.read_bytes(), before)

    def test_preflight_cache_failure_preserves_all_target_bytes(self):
        self.adapter.flush_failure = True
        before = self.adapter.path.read_bytes()
        with self.assertRaisesRegex(flash.Refused, "cache flush"):
            self.execute()
        self.assertEqual(self.adapter.path.read_bytes(), before)

    def test_readback_corruption_fails_without_success(self):
        self.adapter.corrupt = True
        with self.assertRaisesRegex(flash.Refused, "Read-back SHA-256 mismatch"):
            self.execute()
        self.assertEqual(self.adapter.ejects, 0)

    def test_exact_size_target_preserves_all_image_bytes(self):
        self.adapter.path.write_bytes(b"X" * len(self.data))
        self.adapter.disk = replace(self.adapter.disk, size=len(self.data))
        self.execute()
        self.assertEqual(self.adapter.path.read_bytes(), self.data)

    def test_short_remaining_tail_never_changes_image(self):
        self.adapter.path.write_bytes(b"X" * (len(self.data) + 512))
        self.adapter.disk = replace(self.adapter.disk, size=len(self.data) + 512)
        self.execute()
        self.assertEqual(self.adapter.path.read_bytes(), self.data + b"\0" * 512)

    def test_ineligible_explicit_device_remains_blocked(self):
        self.adapter.disk = replace(self.adapter.disk, blocked="system disk")
        with self.assertRaisesRegex(flash.Refused, "system disk"):
            self.execute()
        self.assertEqual(self.adapter.opens, 0)

    def test_partition_and_alias_never_selected(self):
        for path in ("/dev/test-only1", "/dev/disk/by-id/alias", str(self.adapter.path)):
            with self.assertRaisesRegex(flash.Refused, "whole-disk path"):
                flash.checked_disk(self.adapter, path, len(self.data))

    def test_small_disk_refused(self):
        self.adapter.disk = replace(self.adapter.disk, size=512)
        with self.assertRaisesRegex(flash.Refused, "smaller"):
            self.execute()
        self.assertEqual(self.adapter.opens, 0)

    def test_real_device_check_rejects_file_symlink_and_character_device(self):
        link = self.root / "link"
        link.symlink_to(self.raw)
        for path in (self.raw, link, Path("/dev/null")):
            with self.assertRaisesRegex(flash.Refused, "actual whole block"):
                flash.device_identity(path)

    def test_real_fd_check_rejects_regular_file(self):
        with self.raw.open("rb") as source:
            with self.assertRaisesRegex(flash.Refused, "no longer matches"):
                flash.check_fd(source.fileno(), self.adapter.disk)

    def test_local_input_does_not_follow_symlink(self):
        link = self.root / "link"
        link.symlink_to(self.raw)
        with self.assertRaises(OSError):
            flash.regular_input(link)
        with self.assertRaises(flash.Refused):
            flash.regular_input("/dev/null")

    def test_gzip_verified_before_devices_touched(self):
        compressed = self.root / "image.gz"
        compressed.write_bytes(gzip.compress(self.data))
        size, digest = flash.decompress_verified(compressed, self.root / "result.img")
        self.assertEqual((size, digest), (len(self.data), self.sha))
        self.assertEqual(self.adapter.opens, 0)

    def test_truncated_and_crc_bad_gzip_are_rejected(self):
        valid = gzip.compress(self.data)
        invalid = bytearray(valid)
        invalid[-8] ^= 0xFF
        for n, value in enumerate((valid[:-4], bytes(invalid))):
            compressed = self.root / f"bad{n}.gz"
            compressed.write_bytes(value)
            with self.assertRaisesRegex(flash.Refused, "decompression failed"):
                flash.decompress_verified(compressed, self.root / f"bad{n}.img")
            self.assertEqual(self.adapter.opens, 0)

    def test_gzip_size_bomb_is_bounded(self):
        compressed = self.root / "large.gz"
        compressed.write_bytes(gzip.compress(self.data))
        with mock.patch.object(flash, "MAX_RAW", 512):
            with self.assertRaisesRegex(flash.Refused, "32 GiB"):
                flash.decompress_verified(compressed, self.root / "large.img")

    def test_staging_space_failure_prevents_write(self):
        with mock.patch.object(flash.shutil, "disk_usage", return_value=mock.Mock(free=0)):
            with self.assertRaisesRegex(flash.Refused, "staging space"):
                flash.copy_verified(io.BytesIO(self.data), self.root / "download", self.sha)
        self.assertEqual(self.adapter.opens, 0)

    def test_download_hash_and_length_must_match(self):
        for n, (digest, size) in enumerate((("0" * 64, len(self.data)), (self.sha, len(self.data) + 1), (self.sha, 100))):
            with self.assertRaises(flash.Refused):
                flash.copy_verified(io.BytesIO(self.data), self.root / f"download{n}", digest, size)
        self.assertEqual(self.adapter.opens, 0)

    def test_staged_file_truncation_refused_before_tail_clear(self):
        self.raw.write_bytes(b"changed")
        before = self.adapter.path.read_bytes()
        with self.assertRaisesRegex(flash.Refused, "size changed"):
            self.execute()
        self.assertEqual(self.adapter.path.read_bytes(), before)

    def test_same_size_staged_mutation_refused_before_tail_clear(self):
        changed = bytearray(self.data)
        changed[600] ^= 1
        self.raw.write_bytes(changed)
        before = self.adapter.path.read_bytes()
        with self.assertRaisesRegex(flash.Refused, "SHA-256 changed before"):
            self.execute()
        self.assertEqual(self.adapter.path.read_bytes(), before)
        self.assertEqual(self.adapter.ejects, 0)

    def test_partial_write_handled(self):
        original = os.write
        def partial(fd, data):
            return original(fd, data[:131072])
        with mock.patch.object(flash.os, "write", side_effect=partial):
            self.execute()
        self.assertEqual(self.adapter.path.read_bytes()[:len(self.data)], self.data)

    def test_no_unattended_or_force_flag(self):
        for option in ("--yes", "--force", "--allow-file", "--skip-verify"):
            with mock.patch("sys.stderr", new=io.StringIO()), self.assertRaises(SystemExit):
                flash.parser().parse_args([option])

    def test_not_interactive_rejected_before_download(self):
        with mock.patch.object(flash.sys.stdin, "isatty", return_value=False), mock.patch.object(flash, "stage_image") as stage:
            with self.assertRaisesRegex(flash.Refused, "interactive terminal"):
                flash.main(["--target", "amd64"])
            stage.assert_not_called()

    def test_local_image_manifest_end_to_end(self):
        name = "vaultboot-0.1.0-preview.2-amd64.img.gz"
        compressed = self.root / name
        compressed.write_bytes(gzip.compress(self.data))
        manifest = self.root / "SHA256SUMS"
        manifest.write_text(hashlib.sha256(compressed.read_bytes()).hexdigest() + "  " + name + "\n")
        directory = self.root / "private"
        directory.mkdir(mode=0o700)
        args = argparse.Namespace(image=str(compressed), manifest=str(manifest), target="amd64", release=None)
        with mock.patch("sys.stdout", new=io.StringIO()):
            raw, size, digest = flash.stage_image(args, directory)
        self.assertEqual(raw.read_bytes(), self.data)
        self.assertEqual((size, digest), (len(self.data), self.sha))
        self.assertEqual(self.adapter.opens, 0)


class MetadataTests(unittest.TestCase):
    def release(self, tag, published="2026-10-18T20:00:00Z", target="amd64"):
        names = [f"vaultboot-{tag[1:]}-{target}.img.gz", "SHA256SUMS"]
        return {"tag_name": tag, "published_at": published, "draft": False, "prerelease": "preview" in tag,
                "assets": [{"name": name, "state": "uploaded", "size": 1024,
                            "browser_download_url": f"https://github.com/{flash.REPOSITORY}/releases/download/{tag}/{name}"} for name in names]}

    def test_default_discovers_prerelease_and_sorts_publication(self):
        releases = [self.release("v0.1.0", "2026-10-17T00:00:00Z"), self.release("v0.1.0-preview.2")]
        with mock.patch.object(flash, "fetch_small", return_value=json.dumps(releases).encode()) as fetch:
            selected = flash.discover_release("amd64")
        self.assertEqual(selected[0], "v0.1.0-preview.2")
        self.assertIn("/releases?", fetch.call_args.args[0])
        self.assertNotIn("latest", fetch.call_args.args[0])

    def test_draft_or_missing_assets_skipped(self):
        draft = self.release("v1.0.0")
        draft["draft"] = True
        incomplete = self.release("v0.2.0")
        incomplete["assets"] = []
        with mock.patch.object(flash, "fetch_small", return_value=json.dumps([draft, incomplete, self.release("v0.1.0")]).encode()):
            self.assertEqual(flash.discover_release("amd64")[0], "v0.1.0")

    def test_explicit_release_does_not_silently_fall_back(self):
        with mock.patch.object(flash, "fetch_small", return_value=json.dumps(self.release("v0.1.0", target="pi3-arm64")).encode()):
            with self.assertRaises(flash.Refused):
                flash.discover_release("amd64", "v0.1.0")

    def test_asset_from_other_repo_rejected(self):
        release = self.release("v0.1.0")
        release["assets"][0]["browser_download_url"] = "https://github.com/other/other/releases/download/v0.1.0/image.img.gz"
        with self.assertRaisesRegex(flash.Refused, "Unexpected"):
            flash.assets_for(release, "amd64")

    def test_redirects_only_allow_known_https_hosts(self):
        for url in ("http://github.com/file", "https://github.com.evil/file", "https://user@github.com/file", "https://github.com:444/file", "file:///tmp/image"):
            with self.assertRaises(flash.Refused):
                flash.valid_https(url)
        self.assertEqual(flash.valid_https("https://release-assets.githubusercontent.com/file?token=opaque"), "https://release-assets.githubusercontent.com/file?token=opaque")

    def test_manifest_duplicate_missing_malformed_and_traversal_rejected(self):
        line = "a" * 64 + "  image.img.gz\n"
        self.assertEqual(flash.manifest_hash(line.encode(), "image.img.gz"), "a" * 64)
        for body in ((line + line).encode(), b"nonsense", ("a" * 64 + "  ../image.img.gz\n").encode(), line.replace("image", "other").encode()):
            with self.assertRaises(flash.Refused):
                flash.manifest_hash(body, "image.img.gz")


class DevicePolicyTests(unittest.TestCase):
    def setUp(self):
        self.node = {"type": "disk", "path": "/dev/sdz", "maj:min": "8:240", "ro": False, "rm": True,
                     "log-sec": 512, "tran": "usb", "size": 16 * flash.GIB, "fstype": None,
                     "children": [{"type": "part", "maj:min": "8:241", "fstype": "vfat", "mountpoints": []}]}
        self.holders = {"8:240": False, "8:241": False}

    def reason(self, protected=(), mounts=None):
        return flash.linux_reason(self.node, set(protected), mounts or {}, self.holders)

    def test_removable_automount_is_eligible(self):
        self.assertEqual(self.reason(mounts={"8:241": ["/media/user/USB"]}), "")

    def test_root_boot_home_source_and_swap_storage_protected(self):
        for point in ("/", "/boot", "/boot/efi", "/home", "/mnt/manual"):
            self.assertTrue(self.reason(mounts={"8:241": [point]}))
        self.assertIn("source", self.reason(protected={"8:241"}))
        self.node["children"][0]["mountpoints"] = ["[SWAP]"]
        self.assertIn("swap", self.reason())

    def test_hidden_mapper_holder_and_complex_storage_rejected(self):
        self.holders["8:241"] = True
        self.assertIn("held", self.reason())
        self.holders["8:241"] = False
        for kind in ("btrfs", "zfs_member", "linux_raid_member", "LVM2_member", "crypto_LUKS", "swap"):
            self.node["children"][0]["fstype"] = kind
            self.assertTrue(self.reason())

    def test_internal_read_only_unknown_and_4kn_rejected(self):
        for patch in ({"rm": False, "tran": "sata"}, {"ro": True}, {"ro": None}, {"log-sec": 4096}, {"type": "part"}):
            node = {**self.node, **patch}
            self.assertTrue(flash.linux_reason(node, set(), {}, self.holders))

    def test_mount_table_unescapes_paths_and_keeps_multiple_mounts(self):
        text = "1 0 8:241 / /media/user/My\\040USB rw - vfat /dev/sdz1 rw\n2 0 8:241 / /boot rw - vfat /dev/sdz1 rw\n"
        self.assertEqual(flash.linux_mounts(text)["8:241"], ["/media/user/My USB", "/boot"])

    def test_geometry_generation_and_4kn_refused(self):
        disk = flash.Disk("/dev/fake", 8192, "", "", ("/dev/fake", 0, 0, 0, "123"))
        for values in ([8192, 512, 124], [8192, 4096, 123], [4096, 512, 123]):
            with mock.patch.object(flash, "ioctl_integer", side_effect=values):
                with self.assertRaises(flash.Refused):
                    flash.check_geometry(999, disk, "Linux")

    def test_mac_external_plain_usb_only(self):
        info = {"WholeDisk": True, "VirtualOrPhysical": "Physical", "Internal": False, "Writable": True,
                "DeviceBlockSize": 512, "BusProtocol": "USB", "DeviceIdentifier": "disk9", "DeviceTreePath": "IOService:/USB/fixture"}
        self.assertEqual(flash.mac_reason(info, [{"Content": "DOS_FAT_32"}], set(), ("/Volumes/USB",)), "")
        for patch in ({"Internal": True}, {"Writable": False}, {"DeviceBlockSize": 4096}, {"DeviceTreePath": ""}, {"VirtualOrPhysical": "Virtual"}):
            self.assertTrue(flash.mac_reason({**info, **patch}, [], set(), ()))
        for content in ("Apple_APFS", "Apple_CoreStorage", "Apple_RAID"):
            self.assertTrue(flash.mac_reason(info, [{"Content": content}], set(), ()))
        self.assertTrue(flash.mac_reason(info, [], {"disk9"}, ()))
        self.assertTrue(flash.mac_reason(info, [], set(), ("/",)))

    def test_mac_exclusive_driver_open_is_required(self):
        disk = flash.Disk("/dev/fake", 8192, "", "", ())
        with mock.patch.object(flash.os, "O_EXLOCK", 0x20, create=True), \
             mock.patch.object(flash.os, "open", return_value=999) as opened, \
             mock.patch.object(flash, "check_fd"), mock.patch.object(flash, "check_geometry"), \
             mock.patch.object(flash.fcntl, "fcntl"):
            self.assertEqual(flash.MacDisks([]).open(disk), 999)
        self.assertTrue(opened.call_args.args[1] & 0x20)
        self.assertTrue(opened.call_args.args[1] & os.O_NOFOLLOW)


if __name__ == "__main__":
    unittest.main()
