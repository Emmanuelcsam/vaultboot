"""Offline tests: no network, real desktop, power actions, or user stores."""

import hashlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import MagicMock, patch
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("vaultboot_runtime", ROOT / "config/includes.chroot/usr/local/lib/vaultboot/runtime.py")
runtime = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runtime)


def result(stdout="", code=0):
    return subprocess.CompletedProcess([], code, stdout, "")


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.app = runtime.Runtime(self.home)
        self.body = b"fake package bytes; dpkg is mocked"
        self.digest = hashlib.sha256(self.body).hexdigest()
        self.release = {"tag_name": "v1.14.4", "assets": [{"name": "obsidian_1.14.4_amd64.deb", "size": len(self.body), "digest": "sha256:" + self.digest, "browser_download_url": runtime.DOWNLOAD_PREFIX + "v1.14.4/obsidian_1.14.4_amd64.deb"}]}
        self.calls = []
        self.addCleanup(patch.stopall)
        patch.object(runtime.os, "geteuid", return_value=1000).start()
        patch.object(runtime.shutil, "disk_usage", return_value=MagicMock(free=10**12)).start()

    def opener(self, url):
        self.calls.append(url)
        return io.BytesIO(json.dumps(self.release).encode() if url == runtime.RELEASE_API else self.body)

    def extract(self, args, **kwargs):
        self.assertEqual(args[:2], ["dpkg-deb", "--extract"])
        self.assertNotIn("sudo", args)
        base = Path(args[3]) / "opt/Obsidian"
        base.mkdir(parents=True)
        (base / "obsidian").write_text("fake executable")
        (base / "obsidian").chmod(0o755)
        (base / "chrome-sandbox").write_text("fake sandbox")
        (base / "chrome-sandbox").chmod(0o4755)
        return result()

    def old_install(self):
        old = self.app.releases / "old"
        executable = old / "opt/Obsidian/obsidian"
        executable.parent.mkdir(parents=True)
        executable.write_text("old version")
        executable.chmod(0o755)
        self.app.current.symlink_to(old)
        return old

    def test_install_verifies_extracts_and_atomically_replaces_link(self):
        old = self.old_install()
        with patch.object(runtime, "run", side_effect=self.extract):
            asset = self.app.install(self.opener)
        self.assertEqual(asset["sha256"], self.digest)
        self.assertEqual(self.app.executable().read_text(), "fake executable")
        self.assertTrue(old.exists())
        self.assertEqual((self.app.current / "opt/Obsidian/chrome-sandbox").stat().st_mode & 0o7777, 0o755)
        self.assertEqual(json.loads((self.app.current / "release.json").read_text())["sha256"], self.digest)
        self.assertEqual(len(self.calls), 2)
        self.assertFalse(list(self.app.releases.glob(".install-*")))

    def test_checksum_failure_keeps_old_install(self):
        old = self.old_install()
        self.body = b"X" * len(self.body)
        with patch.object(runtime, "run") as command:
            with self.assertRaisesRegex(runtime.SetupError, "checksum"):
                self.app.install(self.opener)
        self.assertEqual(self.app.current.resolve(), old)
        command.assert_not_called()
        self.assertFalse(list(self.app.releases.glob(".install-*")))

    def test_truncated_download_keeps_old_install(self):
        old = self.old_install()
        self.body = self.body[:4]
        with self.assertRaisesRegex(runtime.SetupError, "checksum"):
            self.app.install(self.opener)
        self.assertEqual(self.app.current.resolve(), old)

    def test_extract_failure_keeps_old_install(self):
        old = self.old_install()
        with patch.object(runtime, "run", return_value=result(code=1)):
            with self.assertRaisesRegex(runtime.SetupError, "extracted"):
                self.app.install(self.opener)
        self.assertEqual(self.app.current.resolve(), old)

    def test_atomic_switch_failure_keeps_previous_target(self):
        old = self.old_install()
        with patch.object(runtime, "run", side_effect=self.extract), patch.object(runtime.os, "replace", side_effect=OSError("simulated full device")):
            with self.assertRaises(OSError):
                self.app.install(self.opener)
        self.assertEqual(self.app.current.resolve(), old)
        self.assertFalse(list(self.app.data.glob(".current-*")))

    def test_no_duplicate_download_when_digest_already_installed(self):
        self.old_install()
        (self.app.current / "release.json").write_text(json.dumps({"sha256": self.digest}))
        with patch.object(runtime, "run") as command:
            self.app.install(self.opener)
        self.assertEqual(self.calls, [runtime.RELEASE_API])
        command.assert_not_called()

    def test_update_retains_current_and_previous_without_accumulating_old_releases(self):
        with patch.object(runtime, "run", side_effect=self.extract):
            self.app.install(self.opener)
            first = self.app.current.resolve()
            self.body = b"another verified upstream release"
            self.release["assets"][0].update(size=len(self.body), digest="sha256:" + hashlib.sha256(self.body).hexdigest())
            self.app.install(self.opener)
            second = self.app.current.resolve()
            self.body = b"third verified upstream release"
            self.release["assets"][0].update(size=len(self.body), digest="sha256:" + hashlib.sha256(self.body).hexdigest())
            self.app.install(self.opener)
        self.assertFalse(first.exists())
        self.assertTrue(second.exists())
        self.assertTrue(self.app.current.resolve().exists())
        self.assertEqual(len(list(self.app.releases.iterdir())), 2)

    def test_missing_digest_is_rejected(self):
        self.release["assets"][0]["digest"] = None
        with self.assertRaisesRegex(runtime.SetupError, "SHA-256"):
            self.app.install(self.opener)
        self.assertEqual(self.calls, [runtime.RELEASE_API])

    def test_unexpected_asset_origin_is_rejected(self):
        self.release["assets"][0]["browser_download_url"] = "https://example.org/package.deb"
        with self.assertRaises(runtime.SetupError):
            self.app.install(self.opener)
        self.assertEqual(self.calls, [runtime.RELEASE_API])

    def test_insufficient_space_does_not_download_package(self):
        with patch.object(runtime.shutil, "disk_usage", return_value=MagicMock(free=10)):
            with self.assertRaisesRegex(runtime.SetupError, "space"):
                self.app.install(self.opener)
        self.assertEqual(self.calls, [runtime.RELEASE_API])

    def test_root_install_is_refused(self):
        with patch.object(runtime.os, "geteuid", return_value=0):
            with self.assertRaisesRegex(runtime.SetupError, "never as root"):
                self.app.install(self.opener)
        self.assertEqual(self.calls, [])

    def test_cached_setup_is_offline_and_noninteractive(self):
        self.old_install()
        with patch.object(runtime, "run") as command, patch.object(runtime, "network_open") as network:
            self.assertTrue(self.app.setup())
        command.assert_not_called()
        network.assert_not_called()

    def test_home_partition_is_persistent(self):
        with patch.object(runtime, "run", return_value=result("ext4 rw,relatime\n")):
            self.assertTrue(self.app.is_persistent())

    def test_overlay_upper_on_tmpfs_is_volatile(self):
        with patch.object(runtime, "run", side_effect=[result("overlay rw,lowerdir=/a,upperdir=/run/live/overlay/rw,workdir=/b"), result("tmpfs")]):
            self.assertFalse(self.app.is_persistent())

    def test_overlay_upper_on_disk_is_persistent(self):
        with patch.object(runtime, "run", side_effect=[result("overlay rw,lowerdir=/a,upperdir=/run/live/persistence/vaultboot-data/rw,workdir=/b"), result("ext4")]):
            self.assertTrue(self.app.is_persistent())

    def test_unknown_mount_is_conservatively_volatile(self):
        with patch.object(runtime, "run", return_value=result(code=1)):
            self.assertFalse(self.app.is_persistent())

    def test_volatile_demo_requires_explicit_acceptance_once(self):
        with patch.object(self.app, "is_persistent", return_value=False), patch.object(runtime, "dialog", return_value=result()) as prompt:
            self.assertTrue(self.app.confirm_storage())
            self.assertTrue(self.app.confirm_storage())
        prompt.assert_called_once()
        self.assertIn("disappear", prompt.call_args.args[1])

    def test_rejecting_demo_powers_off_instead_of_opening_app(self):
        with patch.object(self.app, "is_persistent", return_value=False), patch.object(runtime, "dialog", return_value=result(code=1)), patch.object(runtime, "run", return_value=result()) as command:
            self.assertFalse(self.app.confirm_storage())
        command.assert_called_once_with(["systemctl", "poweroff"])
        self.assertFalse((self.app.state / "volatile-accepted").exists())

    def test_notes_initialization_preserves_user_work_and_preferences(self):
        self.app.prepare_notes()
        welcome = self.app.notes / "Welcome to Vaultboot.md"
        self.assertIn("Ctrl+Alt+S", welcome.read_text())
        welcome.write_text("my edits")
        config = self.home / ".config/obsidian/obsidian.json"
        config.write_text("my settings")
        self.app.prepare_notes()
        self.assertEqual(welcome.read_text(), "my edits")
        self.assertEqual(config.read_text(), "my settings")

    def test_sandbox_probe_failure_never_uses_unsafe_flags(self):
        with patch.object(runtime, "run", return_value=result(code=1)) as command:
            with self.assertRaisesRegex(runtime.SetupError, "sandbox"):
                self.app.check_sandbox()
        self.assertEqual(command.call_args.args[0], ["unshare", "--user", "--map-root-user", "true"])

    def test_repeated_short_runs_offer_recovery_and_back_off(self):
        self.old_install()
        child = MagicMock()
        child.wait.return_value = 1
        counter = [0]

        def recovery(args, **kwargs):
            counter[0] += 1
            self.app.stopping = True
            return result("Try again")

        clock = [0]

        def tick():
            clock[0] += 0.1
            return clock[0]

        def sleep(seconds):
            clock[0] += seconds

        with patch.object(self.app, "check_sandbox"), patch.object(self.app, "confirm_storage", return_value=True), patch.object(self.app, "prepare_notes"), patch.object(runtime.signal, "signal"), patch.object(runtime.subprocess, "Popen", return_value=child) as launches, patch.object(runtime, "run", side_effect=recovery), patch.object(runtime.time, "monotonic", side_effect=tick), patch.object(runtime.time, "sleep", side_effect=sleep):
            self.assertEqual(self.app.session(), 0)
        self.assertEqual(launches.call_count, 5)
        self.assertEqual(counter[0], 1)
        self.assertGreater(clock[0], 20)
        for call in launches.call_args_list:
            self.assertNotIn("--no-sandbox", call.args[0])

    def test_backup_contains_notes_and_hidden_vault_settings(self):
        self.app.prepare_notes()
        (self.app.notes / ".obsidian/app.json").write_text("{}")
        destination = self.home / "backup-drive"
        destination.mkdir()
        with patch.object(runtime, "run", return_value=result(str(destination))), patch.object(runtime, "dialog"):
            self.app.backup()
        archives = list(destination.glob("*.tar.gz"))
        self.assertEqual(len(archives), 1)
        with tarfile.open(archives[0]) as archive:
            self.assertIn("Notes/.obsidian/app.json", archive.getnames())

    def test_backup_rejects_destination_inside_vault(self):
        self.app.prepare_notes()
        with patch.object(runtime, "run", return_value=result(str(self.app.notes))), patch.object(runtime, "dialog") as prompt:
            self.app.backup()
        self.assertEqual(prompt.call_args.args[0], "error")
        self.assertFalse(list(self.app.notes.glob("*.tar.gz")))

    def test_openbox_config_exposes_recovery_shortcuts(self):
        root = ET.parse(ROOT / "config/includes.chroot/etc/xdg/openbox/vaultboot-rc.xml")
        namespace = {"ob": "http://openbox.org/3.4/rc"}
        keys = {node.attrib["key"] for node in root.findall(".//ob:keybind", namespace)}
        self.assertTrue({"C-A-s", "C-A-t"}.issubset(keys))

    def test_redirects_require_official_https_hosts(self):
        runtime.check_url("https://release-assets.githubusercontent.com/example?signature=abc")
        for url in ["http://github.com/file", "https://github.com.evil.example/file", "https://example.com/file", "https://token@github.com/file", "https://github.com:444/file"]:
            with self.subTest(url=url), self.assertRaises(runtime.SetupError):
                runtime.check_url(url)


if __name__ == "__main__":
    unittest.main()
