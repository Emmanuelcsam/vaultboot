"""Pi preload refinalization guards, all in isolated temporary Git/root trees."""
import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("preload_overlay", ROOT / "build/preload-overlay.py")
overlay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(overlay)


class PreloadOverlayTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.project = Path(self.temporary.name) / "project"
        self.project.mkdir()
        self.root = Path(self.temporary.name) / "root"
        self.root.mkdir()
        self.git("init", "-q")
        for name in overlay.ALLOWED[:2]:
            self.write(name, "original " + name)
            destination = self.target(name)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text("original " + name)
            destination.chmod(0o644)
        self.build = 'packages unchanged\npython3 "$project/build/pi-layout.py" "$image" > "$output/$prefix.layout.json"\n'
        self.hook = "#!/bin/sh\nsystemctl enable NetworkManager.service lightdm.service systemd-timesyncd.service\n"
        self.write("build/pi.sh", self.build)
        self.write("build/pi-layout.py", "old layout")
        self.write(overlay.HOOK, self.hook)
        for name in overlay.UNCHANGED:
            self.write(name, "fixed package/source inputs")
        self.base = self.commit()
        for name in overlay.ALLOWED:
            self.write(name, "new " + name)
        self.write(overlay.ALLOWED[-1], "[Unit]\nDescription=Fixture\n[Install]\nWantedBy=graphical.target\n")
        self.write("build/pi.sh", self.build.replace("python3", overlay.SLOT_APPEND.decode() + "python3", 1))
        self.write(overlay.HOOK, self.hook.rstrip("\n") + " vaultboot-preload.service\n")
        self.write("build/pi-layout.py", "new declared layout")
        self.write("build/preload-layout.py", "new slot helper")

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.project), *args], stderr=subprocess.PIPE).decode().strip()

    def write(self, name, content):
        path = self.project / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        path.chmod(0o644)

    def commit(self):
        self.git("add", ".")
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "fixture")
        return self.git("rev-parse", "HEAD")

    def target(self, name):
        return self.root / name.removeprefix(overlay.INCLUDES)

    def test_declared_files_and_service_are_installed_with_complete_provenance(self):
        final = self.commit()
        result = overlay.apply(self.project, self.root, self.base, final)
        self.assertEqual(result["base_project_commit"], self.base)
        self.assertEqual(result["preload_overlay_commit"], final)
        self.assertEqual(len(result["changed_files"]), 6)
        self.assertIsNone(result["changed_files"][2]["old_sha256"])
        self.assertTrue(result["effective_pi_packages_unchanged"])
        self.assertEqual(len(result["layout_build_changes"]), 3)
        for name in overlay.ALLOWED:
            self.assertEqual(self.target(name).read_bytes(), (self.project / name).read_bytes())
            self.assertEqual(self.target(name).stat().st_mode & 0o777, 0o644)
        self.assertEqual((self.root / overlay.SERVICE_LINK).readlink(), Path(overlay.SERVICE_TARGET))

    def test_all_original_files_checked_before_first_write(self):
        final = self.commit()
        self.target(overlay.ALLOWED[1]).write_text("changed base installer")
        before = self.target(overlay.ALLOWED[0]).read_bytes()
        with self.assertRaisesRegex(ValueError, "differs from the base"):
            overlay.apply(self.project, self.root, self.base, final)
        self.assertEqual(self.target(overlay.ALLOWED[0]).read_bytes(), before)
        self.assertFalse(self.target(overlay.ALLOWED[2]).exists())

    def test_existing_new_destination_refuses_all_changes(self):
        final = self.commit()
        self.target(overlay.ALLOWED[2]).write_text("unexpected existing preload")
        before = self.target(overlay.ALLOWED[0]).read_bytes()
        with self.assertRaisesRegex(ValueError, "already exists"):
            overlay.apply(self.project, self.root, self.base, final)
        self.assertEqual(self.target(overlay.ALLOWED[0]).read_bytes(), before)

    def test_extra_config_change_refused(self):
        self.write("config/includes.chroot/etc/extra", "unreviewed")
        with self.assertRaisesRegex(ValueError, "exactly the declared"):
            overlay.plan(self.project, self.base, self.commit())

    def test_package_or_source_changes_refused(self):
        for name in overlay.UNCHANGED:
            with self.subTest(name=name):
                self.write(name, "new dependency")
                with self.assertRaisesRegex(ValueError, "full Pi build"):
                    overlay.plan(self.project, self.base, self.commit())
                self.write(name, "fixed package/source inputs")

    def test_other_pi_build_changes_refused(self):
        self.write("build/pi.sh", self.build + "extra filesystem mutation\n")
        with self.assertRaisesRegex(ValueError, "exceed the declared"):
            overlay.plan(self.project, self.base, self.commit())

    def test_other_hook_changes_refused(self):
        self.write(overlay.HOOK, self.hook + "extra hook change\n")
        with self.assertRaisesRegex(ValueError, "Hook changes"):
            overlay.plan(self.project, self.base, self.commit())

    def test_different_service_target_refused(self):
        self.write(overlay.ALLOWED[-1], "[Install]\nWantedBy=multi-user.target\n")
        with self.assertRaisesRegex(ValueError, "graphical target"):
            overlay.plan(self.project, self.base, self.commit())

    def test_destination_parent_symlink_refused(self):
        final = self.commit()
        share = self.root / "usr/share"
        outside = self.root.parent / "outside"
        outside.mkdir()
        share.symlink_to(outside)
        with self.assertRaisesRegex(ValueError, "symlink"):
            overlay.apply(self.project, self.root, self.base, final)
        self.assertEqual(list(outside.iterdir()), [])

    def test_existing_service_symlink_refused(self):
        final = self.commit()
        service = self.root / overlay.SERVICE_LINK
        service.parent.mkdir(parents=True)
        service.symlink_to(overlay.SERVICE_TARGET)
        with self.assertRaisesRegex(ValueError, "symlink"):
            overlay.apply(self.project, self.root, self.base, final)

    def test_dirty_checkout_refused(self):
        final = self.commit()
        self.write(overlay.ALLOWED[0], "uncommitted")
        with self.assertRaisesRegex(ValueError, "dirty"):
            overlay.plan(self.project, self.base, final)

    def test_symlink_git_object_refused(self):
        name = overlay.ALLOWED[2]
        (self.project / name).unlink()
        (self.project / name).symlink_to("runtime.py")
        with self.assertRaisesRegex(ValueError, "ordinary mode-0644"):
            overlay.plan(self.project, self.base, self.commit())


if __name__ == "__main__":
    unittest.main()
