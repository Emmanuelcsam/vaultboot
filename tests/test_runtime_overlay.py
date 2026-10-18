"""Runtime refinalization guards, using temporary git repositories and files only."""
import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('runtime_overlay', ROOT / 'build/runtime-overlay.py')
overlay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(overlay)


class OverlayTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.project = Path(self.temporary.name) / 'project'
        self.project.mkdir()
        self.root = Path(self.temporary.name) / 'image-root'
        self.root.mkdir()
        self.git('init', '-q')
        for filename in overlay.ALLOWED:
            self.write(filename, 'original ' + filename + '\n')
            target = self.root / Path(filename).relative_to('config/includes.chroot')
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((self.project / filename).read_bytes())
        self.write('build/pi.sh', "grep -Ev '^(linux-image-amd64|grub2-common|firmware-test)$'\n")
        self.write('build/pi-layout.py', '# unchanged layout\n')
        self.write('build/pi-sources.py', '# unchanged sources\n')
        self.write('build/source-selection.py', '# unchanged source selections\n')
        self.write(overlay.PACKAGES, '# packages\nbase\nparted\n')
        self.base = self.commit()
        self.write(overlay.ALLOWED[0], 'corrected runtime\n')

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.project), *args], stderr=subprocess.PIPE).decode().strip()

    def write(self, filename, value):
        path = self.project / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value)
        path.chmod(0o644)

    def commit(self):
        self.git('add', '.')
        self.git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'fixture')
        return self.git('rev-parse', 'HEAD')

    def target(self, filename):
        return self.root / Path(filename).relative_to('config/includes.chroot')

    def test_only_runtime_correction_is_applied_and_manifest_records_both_hashes(self):
        final = self.commit()
        before = self.target(overlay.ALLOWED[1]).read_bytes()
        result = overlay.apply(self.project, self.root, self.base, final)
        self.assertEqual(self.target(overlay.ALLOWED[0]).read_text(), 'corrected runtime\n')
        self.assertEqual(self.target(overlay.ALLOWED[1]).read_bytes(), before)
        self.assertEqual(result['base_image_build_commit'], self.base)
        self.assertEqual(result['runtime_overlay_commit'], final)
        self.assertEqual(len(result['changed_files']), 1)
        self.assertNotEqual(result['changed_files'][0]['old_sha256'], result['changed_files'][0]['new_sha256'])

    def test_every_original_validated_before_any_copy(self):
        self.write(overlay.ALLOWED[1], 'corrected installer\n')
        final = self.commit()
        self.target(overlay.ALLOWED[1]).write_text('unexpected original')
        before = self.target(overlay.ALLOWED[0]).read_bytes()
        with self.assertRaisesRegex(ValueError, 'differs from the base'):
            overlay.apply(self.project, self.root, self.base, final)
        self.assertEqual(self.target(overlay.ALLOWED[0]).read_bytes(), before)

    def test_package_and_hook_changes_require_full_build(self):
        for filename in (overlay.PACKAGES, 'config/hooks/live/extra.hook.chroot'):
            with self.subTest(filename=filename):
                self.write(filename, 'new dependency or hook\n')
                final = self.commit()
                with self.assertRaisesRegex(ValueError, 'full build'):
                    overlay.plan(self.project, self.base, final)

    def test_build_script_change_requires_full_build(self):
        self.write('build/pi.sh', 'arbitrary different build\n')
        final = self.commit()
        with self.assertRaisesRegex(ValueError, 'full build'):
            overlay.plan(self.project, self.base, final)

    def test_source_selection_change_requires_full_build(self):
        self.write('build/source-selection.py', 'changed source resolver\n')
        final = self.commit()
        with self.assertRaisesRegex(ValueError, 'full build'):
            overlay.plan(self.project, self.base, final)

    def test_pc_fdisk_addition_without_pi_exclusion_is_rejected(self):
        self.write(overlay.PACKAGES, '# packages\nbase\nfdisk\nparted\n')
        final = self.commit()
        with self.assertRaisesRegex(ValueError, 'Effective Pi package selection changed'):
            overlay.plan(self.project, self.base, final)

    def test_exact_pc_fdisk_addition_and_exclusion_leave_pi_packages_unchanged(self):
        self.write(overlay.PACKAGES, '# packages\nbase\nfdisk\nparted\n')
        self.write('build/pi.sh', "grep -Ev '^(linux-image-amd64|grub2-common|fdisk|firmware-test)$'\n")
        final = self.commit()
        self.assertEqual(len(overlay.plan(self.project, self.base, final)), 1)

    def test_unrelated_config_change_rejected(self):
        self.write('config/includes.chroot/etc/unsafe', 'unreviewed\n')
        final = self.commit()
        with self.assertRaisesRegex(ValueError, 'Only runtime.py and install.py'):
            overlay.plan(self.project, self.base, final)

    def test_symlink_destination_rejected_without_touching_target(self):
        final = self.commit()
        target = self.target(overlay.ALLOWED[0])
        outside = self.root.parent / 'outside'
        outside.write_bytes(target.read_bytes())
        target.unlink()
        target.symlink_to(outside)
        with self.assertRaisesRegex(ValueError, 'symlink'):
            overlay.apply(self.project, self.root, self.base, final)
        self.assertEqual(outside.read_text(), 'original ' + overlay.ALLOWED[0] + '\n')

    def test_dirty_checkout_and_wrong_head_rejected(self):
        final = self.commit()
        with self.assertRaisesRegex(ValueError, 'final source commit'):
            overlay.plan(self.project, self.base, self.base)
        self.write(overlay.ALLOWED[0], 'uncommitted mutation')
        with self.assertRaisesRegex(ValueError, 'dirty'):
            overlay.plan(self.project, self.base, final)


if __name__ == '__main__':
    unittest.main()
