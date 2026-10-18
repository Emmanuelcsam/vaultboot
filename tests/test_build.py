import importlib.util
from pathlib import Path
import tempfile
import unittest
import subprocess
import hashlib

spec = importlib.util.spec_from_file_location(
    "patch_live_build", Path(__file__).resolve().parents[1] / "build" / "patch-live-build.py")
workaround = importlib.util.module_from_spec(spec)
spec.loader.exec_module(workaround)
source_spec = importlib.util.spec_from_file_location(
    "source_selection", Path(__file__).resolve().parents[1] / "build" / "source-selection.py")
selection = importlib.util.module_from_spec(source_spec)
source_spec.loader.exec_module(selection)


class SourceNameWorkaroundTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "source_debian"
        self.original = ('#!/bin/sh\n' + workaround.COLLECTOR_OLD
                         + '\nif [ -n "${LB_BOOTLOADER_EFI}" ]; then\n' + workaround.OLD + '\nfi\n')
        self.path.write_text(self.original)

    def test_known_version_maps_only_the_efi_source_alias(self):
        self.assertTrue(workaround.patch(self.path, workaround.SUPPORTED))
        updated = self.path.read_text()
        self.assertIn('grub-efi) echo "grub2"', updated)
        self.assertIn('*) echo "${LB_BOOTLOADER_EFI}"', updated)

    def test_reapplying_is_idempotent(self):
        workaround.patch(self.path, workaround.SUPPORTED)
        once = self.path.read_bytes()
        self.assertFalse(workaround.patch(self.path, workaround.SUPPORTED))
        self.assertEqual(self.path.read_bytes(), once)

    def test_can_upgrade_the_original_grub_only_patch(self):
        self.path.write_text(self.original.replace(workaround.OLD, workaround.NEW))
        self.assertTrue(workaround.patch(self.path, workaround.SUPPORTED))
        self.assertIn(workaround.COLLECTOR_MARKER, self.path.read_text())

    def test_unreviewed_package_version_leaves_file_unchanged(self):
        with self.assertRaisesRegex(ValueError, "Review"):
            workaround.patch(self.path, "1:future")
        self.assertEqual(self.path.read_text(), self.original)

    def test_unexpected_upstream_content_leaves_file_unchanged(self):
        self.path.write_text("#!/bin/sh\necho upstream-changed\n")
        before = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, "Unexpected"):
            workaround.patch(self.path, workaround.SUPPORTED)
        self.assertEqual(self.path.read_bytes(), before)


class IncorporatedSourceTests(unittest.TestCase):
    def test_signed_kernel_includes_actual_kernel_source(self):
        self.assertEqual(selection.collect_sources(
            'linux-signed-amd64=6.12.113+1\tlinux (= 6.12.113-1)\t\n'),
            ['linux-signed-amd64=6.12.113+1', 'linux=6.12.113-1'])

    def test_static_dependencies_are_exact_and_deduplicated(self):
        self.assertEqual(selection.collect_sources(
            'rust-sequoia-sqv=1.3.0-3\trustc (= 1.85.0+dfsg3-1)\t'
            'rustc (= 1.85.0+dfsg3-1), rust-libc (= 0.2.169-1)\n'),
            ['rust-libc=0.2.169-1', 'rust-sequoia-sqv=1.3.0-3', 'rustc=1.85.0+dfsg3-1'])

    def test_ordinary_packages_need_no_incorporated_sources(self):
        self.assertEqual(selection.collect_sources('bash=5.2.37-2\t\t\n'), ['bash=5.2.37-2'])

    def test_non_exact_or_malformed_dependency_aborts(self):
        for invalid in ('linux (>= 6)', 'linux', 'linux (= 6); bad'):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                selection.collect_sources('base=1\t' + invalid + '\t\n')

    def test_missing_records_abort(self):
        for invalid in ('', '=\t\t\n', 'base=1\n'):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                selection.collect_sources(invalid)

    def test_source_archive_audit_requires_the_exact_version(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'linux.dsc').write_text('Source: linux\nVersion: 6.12.113-1\n')
            selection.verify_sources(['linux=6.12.113-1'], root)
            with self.assertRaisesRegex(ValueError, 'linux=6.12.100-1'):
                selection.verify_sources(['linux=6.12.100-1'], root)


class BootTimeoutTests(unittest.TestCase):
    def test_installer_checksum_covers_complete_live_root_and_missing_source_fails(self):
        hook = Path(__file__).resolve().parents[1] / 'config/hooks/live/0950-install-source.hook.binary'
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'live').mkdir()
            source = root / 'live/filesystem.squashfs'
            source.write_bytes(b'full compressed root fixture')
            subprocess.run(['sh', str(hook)], cwd=root, check=True)
            manifest = root / 'vaultboot-squashfs.sha256'
            self.assertEqual(manifest.read_text(), hashlib.sha256(source.read_bytes()).hexdigest() + '  live/filesystem.squashfs\n')
            source.unlink()
            self.assertNotEqual(subprocess.run(['sh', str(hook)], cwd=root).returncode, 0)

    def test_both_firmware_menus_autoboot_after_five_seconds(self):
        hook = Path(__file__).resolve().parents[1] / 'config/hooks/live/0900-boot-timeout.hook.binary'
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'isolinux').mkdir()
            (root / 'boot/grub').mkdir(parents=True)
            bios = root / 'isolinux/isolinux.cfg'
            uefi = root / 'boot/grub/config.cfg'
            bios.write_text('include menu.cfg\ndefault vesamenu.c32\nprompt 0\ntimeout 0\n')
            uefi.write_text('set default=0\n')
            subprocess.run(['sh', str(hook)], cwd=root, check=True)
            subprocess.run(['sh', str(hook)], cwd=root, check=True)
            self.assertIn('timeout 50\n', bios.read_text())
            self.assertEqual(uefi.read_text().count('set timeout=5\n'), 1)


if __name__ == "__main__":
    unittest.main()
