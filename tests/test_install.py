"""Installer safety checks; no test opens, formats or mounts a real disk."""

import importlib.util
import io
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock


SPEC = importlib.util.spec_from_file_location(
    "vaultboot_install", Path(__file__).resolve().parents[1]
    / "config/includes.chroot/usr/local/lib/vaultboot/install.py")
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


def node(key, name, kind="disk", parents=(), **kwargs):
    value = {"id": key, "name": "/dev/" + name, "type": kind, "parents": set(parents),
             "holders": set(), "backing": "", "diskseq": "101", "ro": False,
             "size": 32 * 1024**3, "serial": "disk-123", "model": "TEST DISK",
             "wwn": "", "syspath": "/sys/devices/test/" + name}
    value.update(kwargs)
    return value


class ProtectionTests(unittest.TestCase):
    def test_partition_mount_protects_whole_disk_and_other_disk_remains_available(self):
        nodes = {"8:0": node("8:0", "sda"), "8:1": node("8:1", "sda1", "part", ["8:0"]),
                 "8:16": node("8:16", "sdb")}
        protected = installer.protected_devices(nodes, {"8:1"}, set(), "8:1")
        self.assertEqual(protected, {"8:0", "8:1"})
        self.assertIn("in use", installer.refusal(nodes["8:0"], protected))
        self.assertEqual(installer.refusal(nodes["8:16"], protected), "")

    def test_loop_backing_chain_protects_live_usb_and_persistence(self):
        nodes = {"8:0": node("8:0", "sda"), "8:1": node("8:1", "sda1", "part", ["8:0"]),
                 "8:3": node("8:3", "sda3", "part", ["8:0"]),
                 "7:0": node("7:0", "loop0", "loop", ["8:1"], backing="/live.iso"),
                 "7:1": node("7:1", "loop1", "loop", ["7:0"], backing="/live/filesystem.squashfs")}
        protected = installer.protected_devices(nodes, {"7:1", "8:3", "0:28"}, set(), "7:0")
        self.assertEqual(protected, set(nodes))

    def test_swap_and_unmounted_active_lvm_raid_holders_protect_all_members(self):
        nodes = {"8:0": node("8:0", "sda", holders={"9:0"}),
                 "8:16": node("8:16", "sdb", holders={"9:0"}),
                 "9:0": node("9:0", "md0", "raid1", ["8:0", "8:16"], holders={"253:0"}),
                 "253:0": node("253:0", "dm-0", "lvm", ["9:0"]),
                 "8:32": node("8:32", "sdc"), "8:33": node("8:33", "sdc1", "part", ["8:32"])}
        protected = installer.protected_devices(nodes, set(), {"8:33"}, "0:31")
        self.assertTrue({"8:0", "8:16", "8:32"}.issubset(protected))
        self.assertTrue(all(installer.refusal(value, protected) for value in nodes.values()))

    def test_unknown_active_device_and_dependency_cycle_fail_closed(self):
        nodes = {"8:0": node("8:0", "sda")}
        with self.assertRaisesRegex(installer.InstallError, "Cannot trace"):
            installer.protected_devices(nodes, {"252:0"}, set(), "0:1")
        nodes["8:0"]["parents"] = {"8:0"}
        with self.assertRaisesRegex(installer.InstallError, "Cyclic"):
            installer.protected_devices(nodes, {"8:0"}, set(), "0:1")

    def test_partition_readonly_small_and_missing_disk_identity_are_refused(self):
        for change in ({"type": "part"}, {"ro": True}, {"size": installer.MIN_SIZE - 1}, {"diskseq": ""}):
            with self.subTest(change=change):
                self.assertTrue(installer.refusal(node("8:0", "sda", **change), set()))

    def test_mount_parser_decodes_spaces_and_rejects_truncated_data(self):
        rows = installer.parse_mounts("21 1 8:1 / /media/My\\040Disk rw - ext4 /dev/sda1 rw\n")
        self.assertEqual(rows, [("8:1", "/media/My Disk", "ext4")])
        with self.assertRaises(installer.InstallError):
            installer.parse_mounts("21 1 8:1 / /media rw - ext4")

    def test_private_namespace_mounts_are_included_and_exit_races_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            proc = Path(directory)
            for pid, ns, device in (("1", "mnt:[1]", "8:1"), ("2", "mnt:[2]", "8:17"),
                                    ("3", "mnt:[1]", "8:1")):
                process = proc / pid
                (process / "ns").mkdir(parents=True)
                (process / "ns/mnt").symlink_to(ns)
                (process / "mountinfo").write_text(f"1 0 {device} / / rw - ext4 /dev/fake rw\n")
            (proc / "4/ns").mkdir(parents=True)  # Process already exited.
            mounts, roots = installer.namespace_mounts(proc)
            self.assertEqual({row[0] for row in mounts}, {"8:1", "8:17"})
            self.assertEqual(len(roots), 2)

    def test_deleted_or_unresolvable_backing_file_fails_closed(self):
        with self.assertRaises(installer.InstallError):
            installer.backing_device("/old/image (deleted)", [])
        with self.assertRaises(installer.InstallError):
            installer.backing_device("/definitely-missing-vaultboot-test-image", [])

    def test_anonymous_btrfs_protects_every_member_and_unknown_storage_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, identity in (("sda1", "8:1"), ("sdb1", "8:17")):
                device = root / "filesystem-uuid/devices" / name
                device.mkdir(parents=True)
                (device / "dev").write_text(identity)
            mounts = [("0:29", "/", "overlay"), ("0:30", "/data", "btrfs")]
            self.assertEqual(installer.mounted_devices(mounts, root), {"8:1", "8:17"})
            with self.assertRaisesRegex(installer.InstallError, "Cannot trace mounted zfs"):
                installer.mounted_devices([("0:31", "/data", "zfs")], root)
            with self.assertRaisesRegex(installer.InstallError, "Cannot trace mounted fuseblk"):
                installer.mounted_devices([("0:32", "/data", "fuseblk")], root)

    def test_target_swap_hotplug_or_serial_change_invalidates_confirmation(self):
        old = node("8:16", "sdb")
        changes = ({}, {"8:16": dict(old, diskseq="102")}, {"8:16": dict(old, serial="replacement")})
        for nodes in changes:
            with self.subTest(nodes=nodes), mock.patch.object(installer, "inspect_disks", return_value=(nodes, set())):
                with self.assertRaisesRegex(installer.InstallError, "disappeared or changed"):
                    installer.revalidate(old)
        with mock.patch.object(installer, "inspect_disks", return_value=({"8:16": old}, {"8:16"})):
            with self.assertRaisesRegex(installer.InstallError, "now unsafe"):
                installer.revalidate(old)

    def test_symlink_device_path_is_rejected(self):
        with mock.patch.object(Path, "lstat", return_value=mock.Mock(st_mode=stat.S_IFLNK)):
            with self.assertRaisesRegex(installer.InstallError, "same whole block device"):
                installer.check_device_node(node("8:16", "sdb"))

    def test_exact_disk_specific_typed_confirmation_is_required(self):
        disk = node("8:16", "sdb")
        phrase = installer.confirmation(disk, "ABCDEF")
        self.assertIn("/dev/sdb", phrase)
        self.assertIn(str(disk["size"]), phrase)
        with mock.patch("builtins.input", side_effect=["1", phrase]), \
             mock.patch.object(installer.secrets, "token_hex", return_value="abcdef"), \
             mock.patch("sys.stdout", new_callable=io.StringIO):
            self.assertIs(installer.choose_disk({"8:16": disk}, set()), disk)
        for wrong in ("yes", "ERASE /dev/sda", phrase + " ", ""):
            with self.subTest(answer=wrong), mock.patch("builtins.input", side_effect=["1", wrong]), \
                 mock.patch.object(installer.secrets, "token_hex", return_value="abcdef"), \
                 mock.patch("sys.stdout", new_callable=io.StringIO):
                with self.assertRaisesRegex(installer.InstallError, "did not match"):
                    installer.choose_disk({"8:16": disk}, set())

    def test_unattended_flag_is_rejected_without_probing_disks(self):
        with mock.patch.object(installer, "preflight") as probe, mock.patch("sys.stderr", new_callable=io.StringIO):
            self.assertEqual(installer.main(["--yes"]), 2)
        probe.assert_not_called()

    def test_terminal_escape_in_disk_model_is_not_rendered(self):
        text = installer.display(node("8:16", "sdb", model="disk\x1b[2J\nWARNING"))
        self.assertNotIn("\x1b", text)
        self.assertNotIn("\n", text)


class RootSetupTests(unittest.TestCase):
    def test_fresh_root_uses_uuids_serial_console_and_no_live_or_personal_data(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "home/writer/Notes").mkdir(parents=True)
            (root / "home/writer/Notes/should-not-transfer.md").write_text("fixture only")
            (root / "etc/default/grub.d").mkdir(parents=True)
            (root / "etc/default/grub.d/live.cfg").write_text('GRUB_CMDLINE_LINUX="boot=live persistence"')
            (root / "etc/resolv.conf").symlink_to("/run/live/old-resolver")
            installer.configure_root(root, "1234-5678", "ABCD-1234")
            fstab = (root / "etc/fstab").read_text()
            grub = (root / "etc/default/grub").read_text()
            self.assertIn("UUID=1234-5678 / ext4", fstab)
            self.assertIn("UUID=ABCD-1234 /boot/efi vfat", fstab)
            self.assertNotIn("/dev/sd", fstab)
            self.assertIn("console=tty0 console=ttyS0,115200", grub)
            self.assertNotIn("boot=live", grub)
            self.assertNotIn("persistence", grub)
            self.assertFalse((root / "etc/default/grub.d").exists())
            self.assertFalse((root / "home/writer").exists())
            self.assertEqual((root / "etc/resolv.conf").readlink(), Path("/run/NetworkManager/resolv.conf"))

    def test_stale_target_refusal_occurs_before_any_destructive_command(self):
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(installer, "source_fingerprint", return_value=(1, 2, 3, 4)), \
             mock.patch.object(installer.tempfile, "mkdtemp", return_value=directory), \
             mock.patch.object(installer, "revalidate", side_effect=installer.InstallError("changed")), \
             mock.patch.object(installer, "run") as command:
            with self.assertRaisesRegex(installer.InstallError, "changed"):
                installer.install(node("8:16", "sdb"), verified_source=(1, 2, 3, 4))
            command.assert_not_called()

    def test_chroot_bootloader_plan_has_both_modes_no_nvram_and_offline_purge(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for child in ("etc", "boot/grub", "boot/efi/EFI/BOOT", "usr/sbin"):
                (root / child).mkdir(parents=True)
            (root / "etc/passwd").write_text("root:x:0:0:root:/root:/bin/bash\n")
            (root / "etc/group").write_text("sudo:x:27:\n")
            (root / "boot/grub/grub.cfg").write_text(" linux /vmlinuz root=UUID=foo ro\n")
            (root / "boot/efi/EFI/BOOT/BOOTX64.EFI").touch()
            with mock.patch.object(installer, "run", return_value="") as commands:
                guard = mock.Mock()
                installer.chroot_setup(root, node("8:16", "sdb"), guard)
            calls = [call.args[2:] for call in commands.call_args_list]
            purge = next(call for call in calls if call[0] == "apt-get")
            self.assertIn("--no-download", purge)
            self.assertTrue(set(installer.LIVE_PACKAGES).issubset(purge))
            bios = next(call for call in calls if "--target=i386-pc" in call)
            efi = next(call for call in calls if "--target=x86_64-efi" in call)
            self.assertEqual(bios[-1], "/dev/sdb")
            self.assertIn("--no-nvram", efi)
            self.assertIn("--removable", efi)
            self.assertIn(("update-initramfs", "-u", "-k", "all"), calls)
            self.assertIn(("update-grub",), calls)
            self.assertFalse((root / "usr/sbin/policy-rc.d").exists())
            self.assertEqual(guard.check.call_count, 2)

    def test_source_hash_checks_entire_file_and_rejects_corruption_before_install(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "filesystem.squashfs"
            manifest = Path(directory) / "manifest"
            data = b"x" * (2 * 1024**2)
            source.write_bytes(data)
            manifest.write_text(hashlib.sha256(data).hexdigest() + "  live/filesystem.squashfs\n")
            with mock.patch("sys.stdout", new_callable=io.StringIO):
                verified = installer.verify_source(source, manifest)
                self.assertEqual(verified, installer.source_fingerprint(source))
                with source.open("r+b") as stream:
                    stream.seek(-1, os.SEEK_END)
                    stream.write(b"y")
                with self.assertRaisesRegex(installer.InstallError, "failed its SHA256"):
                    installer.verify_source(source, manifest)
            with mock.patch.object(installer, "run") as command:
                with self.assertRaisesRegex(installer.InstallError, "verified, unchanged"):
                    installer.install(node("8:16", "sdb"), source, verified)
                command.assert_not_called()

    def test_replaced_disk_is_refused_even_after_our_target_mount_exists(self):
        selected = node("8:16", "sdb")
        with mock.patch.object(installer, "block_inventory", return_value={"8:16": dict(selected, diskseq="999")}):
            with self.assertRaisesRegex(installer.InstallError, "identity changed"):
                installer.check_identity(selected, 123)

    def test_exclusive_probe_rejection_closes_held_descriptor(self):
        selected = node("8:16", "sdb")
        with mock.patch.object(installer, "check_device_node"), \
             mock.patch.object(installer, "check_identity"), \
             mock.patch.object(installer.os, "open", side_effect=[100, OSError("busy")]), \
             mock.patch.object(installer.os, "close") as close, \
             mock.patch.object(installer.fcntl, "flock"):
            with self.assertRaisesRegex(OSError, "busy"):
                with installer.TargetGuard(selected):
                    self.fail("busy disk entered guarded write section")
            close.assert_called_once_with(100)


if __name__ == "__main__":
    unittest.main()
