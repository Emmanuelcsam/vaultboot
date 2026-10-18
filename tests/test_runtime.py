"""Offline tests: no network, real desktop, power actions, or user stores."""

import hashlib
import importlib.util
import io
import json
from contextlib import contextmanager, nullcontext
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("vaultboot_runtime", ROOT / "config/includes.chroot/usr/local/lib/vaultboot/runtime.py")
runtime = importlib.util.module_from_spec(SPEC)
sys.path.insert(0, str(Path(SPEC.origin).parent))
try:
    SPEC.loader.exec_module(runtime)
finally:
    sys.path.pop(0)


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
        patch.object(runtime, "PRELOAD_ERROR", self.home / "offline-error.txt").start()
        patch.object(runtime.preload, "open_cache", side_effect=self.empty_cache).start()
        patch.object(runtime.urllib.request, "build_opener", side_effect=AssertionError("Unit tests must not access the network")).start()

    @staticmethod
    @contextmanager
    def empty_cache(_arch):
        yield None

    def cached(self):
        return SimpleNamespace(asset=runtime.select_asset(self.release), stream=io.BytesIO(self.body),
                               receipt={"terms_accepted": True})

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

    def test_arm64_selects_its_own_official_package_and_checks_digest(self):
        asset = dict(self.release["assets"][0])
        asset["name"] = "obsidian_1.14.4_arm64.deb"
        asset["browser_download_url"] = runtime.DOWNLOAD_PREFIX + "v1.14.4/" + asset["name"]
        self.release["assets"].append(asset)
        with patch.object(runtime.platform, "machine", return_value="aarch64"), patch.object(runtime, "run", side_effect=self.extract):
            installed = self.app.install(self.opener)
        self.assertEqual(installed["architecture"], "arm64")
        self.assertEqual(self.calls[-1], asset["browser_download_url"])
        self.assertEqual(installed["sha256"], self.digest)

    def test_arm64_never_silently_installs_amd64_asset(self):
        with patch.object(runtime.platform, "machine", return_value="aarch64"):
            with self.assertRaisesRegex(runtime.SetupError, "No supported arm64"):
                self.app.install(self.opener)
        self.assertEqual(self.calls, [runtime.RELEASE_API])

    def test_32_bit_arm_requires_a_64_bit_operating_system(self):
        with patch.object(runtime.platform, "machine", return_value="armv7l"):
            with self.assertRaisesRegex(runtime.SetupError, "64-bit"):
                runtime.debian_architecture()

    def make_tar(self, extra=None):
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode="w:gz") as archive:
            executable = tarfile.TarInfo("obsidian-1.14.4-arm64/obsidian")
            executable.mode = 0o755
            data = b"fake ARM64 executable"
            executable.size = len(data)
            archive.addfile(executable, io.BytesIO(data))
            if extra is not None:
                archive.addfile(extra, io.BytesIO(b"x") if extra.isfile() else None)
        return output.getvalue()

    def test_arm64_tar_installs_transactionally_without_dpkg(self):
        self.old_install()
        self.body = self.make_tar()
        asset = self.release["assets"][0]
        asset.update(name="obsidian-1.14.4-arm64.tar.gz", size=len(self.body), digest="sha256:" + hashlib.sha256(self.body).hexdigest())
        asset["browser_download_url"] = runtime.DOWNLOAD_PREFIX + "v1.14.4/" + asset["name"]
        with patch.object(runtime.platform, "machine", return_value="aarch64"), patch.object(runtime, "run") as commands:
            installed = self.app.install(self.opener)
        commands.assert_not_called()
        self.assertEqual(installed["format"], "tar")
        self.assertEqual(self.app.executable().read_bytes(), b"fake ARM64 executable")

    def test_tar_rejects_path_traversal_absolute_paths_links_and_special_files(self):
        cases = [
            ("../outside", tarfile.REGTYPE, ""),
            ("/absolute", tarfile.REGTYPE, ""),
            ("obsidian-1.14.4-arm64/link", tarfile.SYMTYPE, "../../outside"),
            ("obsidian-1.14.4-arm64/link", tarfile.SYMTYPE, "/outside"),
            ("obsidian-1.14.4-arm64/link", tarfile.LNKTYPE, "../outside"),
            ("obsidian-1.14.4-arm64/device", tarfile.CHRTYPE, ""),
        ]
        for index, (name, kind, target) in enumerate(cases):
            with self.subTest(name=name, kind=kind):
                member = tarfile.TarInfo(name)
                member.type = kind
                member.linkname = target
                member.size = 1 if kind == tarfile.REGTYPE else 0
                staging = self.home / ("case" + str(index))
                staging.mkdir()
                archive = staging / "app.tar.gz"
                archive.write_bytes(self.make_tar(member))
                with self.assertRaises(runtime.SetupError):
                    runtime.extract_tar(archive, staging / "extracted")
                self.assertFalse((self.home / "outside").exists())

    def test_tar_preserves_safe_internal_symlinks(self):
        member = tarfile.TarInfo("obsidian-1.14.4-arm64/application-link")
        member.type = tarfile.SYMTYPE
        member.linkname = "obsidian"
        archive = self.home / "app.tar.gz"
        archive.write_bytes(self.make_tar(member))
        runtime.extract_tar(archive, self.home / "extracted")
        linked = self.home / "extracted/opt/Obsidian/application-link"
        self.assertTrue(linked.is_symlink())
        self.assertEqual(linked.read_bytes(), b"fake ARM64 executable")

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
        runtime.preload.open_cache.assert_not_called()

    def test_prepared_install_rehashes_local_bytes_and_preserves_atomic_update(self):
        old = self.old_install()
        cached = self.cached()
        with patch.object(runtime, "run", side_effect=self.extract):
            asset = self.app.install(cached=cached)
        self.assertEqual(asset["sha256"], self.digest)
        self.assertEqual(self.app.executable().read_text(), "fake executable")
        self.assertTrue(old.exists())
        self.assertFalse(cached.stream.closed)
        self.assertFalse(list(self.app.releases.glob(".install-*")))
        self.assertEqual(self.calls, [])

    def test_corrupt_prepared_bytes_keep_existing_app(self):
        old = self.old_install()
        cached = self.cached()
        cached.stream = io.BytesIO(b"X" * len(self.body))
        with patch.object(runtime, "run") as extract:
            with self.assertRaisesRegex(runtime.SetupError, "checksum"):
                self.app.install(cached=cached)
        extract.assert_not_called()
        self.assertEqual(self.app.current.resolve(), old)
        self.assertFalse(list(self.app.releases.glob(".install-*")))

    def test_wrong_architecture_prepared_package_is_rejected(self):
        cached = self.cached()
        with patch.object(runtime.platform, "machine", return_value="aarch64"), patch.object(runtime, "run") as extract:
            with self.assertRaisesRegex(runtime.SetupError, "another computer architecture"):
                self.app.install(cached=cached)
        extract.assert_not_called()

    def test_prepared_arm64_tar_uses_safe_normal_user_extraction(self):
        body = self.make_tar()
        asset = {"version": "v1.14.4", "architecture": "arm64", "format": "tar",
                 "name": "obsidian-1.14.4-arm64.tar.gz", "size": len(body),
                 "sha256": hashlib.sha256(body).hexdigest(), "url": "unused-local-source"}
        with patch.object(runtime.platform, "machine", return_value="aarch64"), patch.object(runtime, "run") as command:
            self.app.install(cached=SimpleNamespace(asset=asset, stream=io.BytesIO(body)))
        command.assert_not_called()
        self.assertEqual(self.app.executable().read_bytes(), b"fake ARM64 executable")

    def test_accepted_prepared_media_installs_without_setup_prompt_or_network(self):
        cached = self.cached()
        progress = MagicMock()
        progress.poll.return_value = None
        with patch.object(runtime.preload, "open_cache", return_value=nullcontext(cached)), patch.object(runtime, "run", side_effect=self.extract), patch.object(runtime.subprocess, "Popen", return_value=progress) as process, patch.object(self.app, "choose") as choose:
            self.assertTrue(self.app.setup())
        choose.assert_not_called()
        self.assertTrue(self.app.executable())
        accepted = json.loads((self.app.state / "prepared-accepted.json").read_text())
        self.assertEqual(accepted["sha256"], self.digest)
        self.assertTrue(accepted["terms_accepted"])
        self.assertIn("No internet connection is needed", " ".join(process.call_args.args[0]))
        progress.terminate.assert_called_once()

    def test_unaccepted_prepared_media_does_not_install(self):
        cached = self.cached()
        cached.receipt = {"terms_accepted": False}
        with patch.object(runtime.preload, "open_cache", return_value=nullcontext(cached)), patch.object(self.app, "download_ui") as install, patch.object(runtime, "dialog") as prompt:
            self.assertEqual(self.app.prepared_setup(), "failed")
        install.assert_not_called()
        self.assertIn("acceptance", prompt.call_args.args[1])
        self.assertFalse((self.app.state / "prepared-accepted.json").exists())

    def test_prepared_extraction_failure_records_no_acceptance_and_offers_retry(self):
        cached = self.cached()
        with patch.object(runtime.preload, "open_cache", return_value=nullcontext(cached)), patch.object(self.app, "download_ui", return_value=False), patch.object(self.app, "choose", return_value="Power off") as choose, patch.object(self.app, "power", return_value=True):
            self.assertFalse(self.app.setup())
        self.assertIn("Retry prepared Obsidian", choose.call_args.args[2])
        self.assertFalse((self.app.state / "prepared-accepted.json").exists())

    def test_bad_prepared_cache_is_visible_without_automatic_network_fallback(self):
        with patch.object(runtime.preload, "open_cache", side_effect=runtime.preload.PreloadError("wrong checksum")), patch.object(runtime, "dialog") as prompt, patch.object(self.app, "choose", return_value="Power off"), patch.object(self.app, "power", return_value=True), patch.object(self.app, "download_ui") as install:
            self.assertFalse(self.app.setup())
        install.assert_not_called()
        self.assertIn("wrong checksum", prompt.call_args.args[1])

    def test_boot_preload_failure_is_visible_in_normal_session(self):
        runtime.PRELOAD_ERROR.write_text("The USB copy failed its checksum.")
        with patch.object(runtime, "dialog") as prompt:
            self.assertEqual(self.app.prepared_setup(), "failed")
        self.assertIn("USB copy failed", prompt.call_args.args[1])

    def test_prepared_retry_can_succeed_without_online_install(self):
        with patch.object(self.app, "prepared_setup", side_effect=["failed", "installed"]), patch.object(self.app, "choose", return_value="Retry prepared Obsidian"), patch.object(self.app, "download_ui") as online:
            self.assertTrue(self.app.setup())
        online.assert_not_called()

    def test_online_update_ignores_prepared_cache(self):
        self.old_install()
        with patch.object(runtime, "run", side_effect=self.extract):
            self.app.install(self.opener)
        self.assertEqual(self.calls, [runtime.RELEASE_API, self.release["assets"][0]["browser_download_url"]])
        runtime.preload.open_cache.assert_not_called()

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

    def test_unresponsive_child_is_killed_after_restart_grace_period(self):
        self.app.child = MagicMock()
        self.app.child.wait.side_effect = [subprocess.TimeoutExpired("obsidian", 0.5), subprocess.TimeoutExpired("obsidian", 0.5), -9]
        self.app.restart_requested = True
        with patch.object(runtime.time, "monotonic", side_effect=[10, 19]), patch.object(self.app, "signal_child") as terminate:
            self.assertEqual(self.app.wait_for_child(), -9)
        terminate.assert_called_once_with(runtime.signal.SIGKILL)

    def test_log_rotation_preserves_inode_for_running_child(self):
        log = self.app.state / "obsidian.log"
        log.write_bytes(b"x" * 4_000_100)
        inode = log.stat().st_ino
        self.app.rotate_app_log()
        self.assertEqual(log.stat().st_ino, inode)
        self.assertEqual(log.stat().st_size, 0)
        self.assertEqual((self.app.state / "obsidian.log.1").stat().st_size, 4_000_000)

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

    def test_failed_network_service_is_visible_and_connection_editor_can_fail_safely(self):
        with patch.object(runtime, "probe", return_value=result(code=1)), patch.object(self.app, "choose", return_value="Connect to Wi-Fi / Ethernet"), patch.object(runtime, "run", return_value=result(code=1)) as command, patch.object(runtime, "dialog") as prompt:
            self.assertIn("unavailable", self.app.network_summary())
            self.app.network()
        self.assertEqual(command.call_args.args[0][-1], "nmtui-connect")
        self.assertTrue(any(call.args[0] == "error" for call in prompt.call_args_list))
        self.assertIn("works offline", prompt.call_args.args[1])

    def test_setup_network_and_keyboard_return_to_download(self):
        with patch.object(self.app, "executable", return_value=None), patch.object(self.app, "choose", side_effect=["Wi-Fi / Ethernet", "Keyboard layout", "Download Obsidian"]), patch.object(self.app, "network") as network, patch.object(self.app, "keyboard") as keyboard, patch.object(self.app, "download_ui", return_value=True):
            self.assertTrue(self.app.setup())
        network.assert_called_once()
        keyboard.assert_called_once()

    def test_list_guidance_wraps_without_losing_words_or_paragraphs(self):
        guidance = ("Use Keyboard layout before entering a Wi-Fi password. " * 4).strip() + "\n\nhttps://obsidian.md/terms"
        with patch.object(runtime, "run", return_value=result("Continue")) as command:
            self.assertEqual(self.app.choose("Set up Vaultboot", guidance, ["Continue"]), "Continue")
        shown = next(value[7:] for value in command.call_args.args[0] if value.startswith("--text="))
        self.assertEqual(shown.split(), guidance.split())
        self.assertLessEqual(max(map(len, shown.splitlines())), 76)
        self.assertIn("\n\nhttps://obsidian.md/terms", shown)

    def test_display_parser_excludes_disconnected_ports_and_marks_current_mode(self):
        output = "Screen 0: minimum 8 x 8\neDP-1 connected primary 1920x1080+0+0\n   1920x1080 60.00*+ 59.90\n   1280x720 60.00\nHDMI-1 disconnected\nDP-1 connected\n   1024x768 60.00+\n"
        displays = runtime.display_outputs(output)
        self.assertEqual(displays["eDP-1"], {"modes": ["1920x1080", "1280x720"], "current": "1920x1080"})
        self.assertIsNone(displays["DP-1"]["current"])
        self.assertNotIn("HDMI-1", displays)

    def test_display_timeout_restores_old_mode_without_saving_bad_setting(self):
        modes = "eDP-1 connected 1920x1080+0+0\n   1920x1080 60*+\n   1280x720 60\n"
        with patch.object(runtime, "probe", side_effect=[result(modes), result()]) as command, patch.object(self.app, "choose", side_effect=["eDP-1", "1280x720"]), patch.object(self.app, "checked", return_value=True), patch.object(runtime, "dialog", return_value=result(code=5)):
            self.app.display()
        self.assertEqual(command.call_args.args[0], ["xrandr", "--output", "eDP-1", "--mode", "1920x1080"])
        self.assertNotIn("display", self.app.settings())

    def test_display_confirmed_mode_is_saved_but_failed_apply_is_not(self):
        modes = "eDP-1 connected 1920x1080+0+0\n   1920x1080 60*+\n   1280x720 60\n"
        with patch.object(runtime, "probe", return_value=result(modes)), patch.object(self.app, "choose", side_effect=["eDP-1", "1280x720"]), patch.object(self.app, "checked", return_value=True), patch.object(runtime, "dialog", return_value=result()):
            self.app.display()
        self.assertEqual(self.app.settings()["display"], {"output": "eDP-1", "mode": "1280x720"})
        with patch.object(runtime, "probe", return_value=result(modes)), patch.object(self.app, "choose", side_effect=["eDP-1", "1280x720"]), patch.object(self.app, "checked", return_value=False), patch.object(runtime, "dialog") as prompt:
            self.app.display()
        prompt.assert_not_called()

    def test_display_restore_failure_explains_manual_recovery(self):
        modes = "eDP-1 connected 1920x1080+0+0\n   1920x1080 60*+\n   1280x720 60\n"
        with patch.object(runtime, "probe", side_effect=[result(modes), result(code=1)]), patch.object(self.app, "choose", side_effect=["eDP-1", "1280x720"]), patch.object(self.app, "checked", return_value=True), patch.object(runtime, "dialog", return_value=result(code=5)) as prompt:
            self.app.display()
        self.assertEqual(prompt.call_args.args[0], "error")
        self.assertIn("xrandr --auto", prompt.call_args.args[1])

    def test_saved_keyboard_and_brightness_apply_but_missing_display_is_skipped(self):
        self.app.save_setting("keyboard", {"layout": "gb", "variant": ""})
        self.app.save_setting("display", {"output": "old-monitor", "mode": "1920x1080"})
        self.app.save_setting("brightness", 65)
        with patch.object(runtime, "probe", return_value=result("eDP-1 connected\n   1024x768 60*\n")) as command:
            self.app.apply_settings()
        calls = [call.args[0] for call in command.call_args_list]
        self.assertIn(["setxkbmap", "-layout", "gb", "-variant", ""], calls)
        self.assertIn(["sudo", "-n", "brightnessctl", "-c", "backlight", "set", "65%"], calls)
        self.assertFalse(any("old-monitor" in call for call in calls))

    def test_malformed_saved_settings_do_not_launch_invalid_commands(self):
        for settings in ([], {"keyboard": {"layout": ["gb"]}, "display": [], "brightness": -1}, {"keyboard": {"layout": "--help"}, "brightness": "20%"}):
            (self.app.data / "settings.json").write_text(json.dumps(settings))
            with patch.object(runtime, "probe") as command:
                self.app.apply_settings()
            command.assert_not_called()

    def test_keyboard_variant_is_applied_and_saved_without_changing_host_console(self):
        rules = self.home / "keyboard.xml"
        rules.write_text('<xkbConfigRegistry><layoutList><layout><configItem><name>us</name><description>English (US)</description></configItem><variantList><variant><configItem><name>intl</name><description>International</description></configItem></variant></variantList></layout></layoutList></xkbConfigRegistry>')
        with patch.object(runtime, "KEYBOARD_RULES", rules), patch.object(self.app, "choose", side_effect=["English (US) (us)", "International"]), patch.object(self.app, "checked", return_value=True) as command, patch.object(runtime, "dialog"):
            self.app.keyboard()
        self.assertEqual(command.call_args.args[0], ["setxkbmap", "-layout", "us", "-variant", "intl"])
        self.assertEqual(self.app.settings()["keyboard"], {"layout": "us", "variant": "intl"})

    def test_missing_backlight_is_explained_without_trying_to_set_it(self):
        with patch.object(runtime, "probe", return_value=result(code=1)) as command, patch.object(runtime, "dialog") as prompt:
            self.app.brightness()
        command.assert_called_once()
        self.assertIn("physical brightness buttons", prompt.call_args.args[1])

    def test_brightness_keeps_a_visible_minimum_and_saves_success_only(self):
        with patch.object(runtime, "probe", return_value=result("intel_backlight,backlight,500,50%,1000\n")), patch.object(runtime, "dialog", return_value=result("5")), patch.object(self.app, "checked", return_value=True) as command:
            self.app.brightness()
        self.assertEqual(self.app.settings()["brightness"], 5)
        self.assertEqual(command.call_args.args[0], ["sudo", "-n", "brightnessctl", "-c", "backlight", "set", "5%"])
        with patch.object(runtime, "probe", return_value=result("intel_backlight,backlight,500,50%,1000\n")), patch.object(runtime, "dialog", return_value=result("90")), patch.object(self.app, "checked", return_value=False):
            self.app.brightness()
        self.assertEqual(self.app.settings()["brightness"], 5)
        with patch.object(runtime, "probe", return_value=result("intel_backlight,backlight,500,50%,1000\n")), patch.object(runtime, "dialog", return_value=result("0")), patch.object(self.app, "checked") as command:
            self.app.brightness()
        command.assert_not_called()

    def test_battery_status_reads_guest_sysfs_and_tolerates_missing_devices(self):
        sysfs = self.home / "power_supply"
        battery = sysfs / "BAT0"
        battery.mkdir(parents=True)
        for name, content in {"type": "Battery", "capacity": "73", "status": "Discharging"}.items():
            (battery / name).write_text(content)
        with patch.object(runtime, "POWER_SUPPLIES", sysfs):
            self.assertIn("73% (Discharging)", runtime.battery_status())
        with patch.object(runtime, "POWER_SUPPLIES", self.home / "missing"):
            self.assertIn("not reported", runtime.battery_status())

    def test_installer_requires_amd64_live_media_and_delegates_confirmation(self):
        image = self.home / "filesystem.squashfs"
        image.touch()
        with patch.object(runtime, "LIVE_SYSTEM", image), patch.object(runtime.platform, "machine", return_value="aarch64"), patch.object(runtime, "dialog"), patch.object(self.app, "launch") as launch:
            self.app.install_disk()
            launch.assert_not_called()
        with patch.object(runtime, "LIVE_SYSTEM", self.home / "absent"), patch.object(runtime.platform, "machine", return_value="x86_64"):
            self.assertFalse(self.app.can_install())
        with patch.object(runtime, "LIVE_SYSTEM", image), patch.object(runtime.platform, "machine", return_value="x86_64"), patch.object(self.app, "launch") as launch:
            self.app.install_disk()
        self.assertEqual(launch.call_args.args[0][-3:], ["-e", "sudo", "/usr/local/sbin/vaultboot-install"])

    def test_cancelled_suspend_and_package_updates_run_nothing(self):
        with patch.object(runtime, "dialog", return_value=result(code=1)), patch.object(runtime, "run") as command, patch.object(self.app, "launch") as launch:
            self.assertFalse(self.app.power("suspend"))
            self.app.system_updates()
        command.assert_not_called()
        launch.assert_not_called()

    def test_suspend_denial_is_visible_and_timezone_command_is_scoped(self):
        with patch.object(runtime, "dialog", return_value=result()) as prompt, patch.object(runtime, "run", return_value=result(code=1)):
            self.assertFalse(self.app.power("suspend"))
        self.assertEqual(prompt.call_args.args[0], "error")
        with patch.object(runtime, "probe", side_effect=[result("America/New_York\nUTC\n"), result("UTC\n")]), patch.object(self.app, "choose", return_value="America/New_York"), patch.object(self.app, "checked", return_value=True) as command, patch.object(runtime, "dialog"):
            self.app.timezone()
        self.assertEqual(command.call_args.args[0], ["timedatectl", "--no-ask-password", "set-timezone", "America/New_York"])

    def test_probe_timeout_is_a_visible_failure_not_a_menu_crash(self):
        with patch.object(runtime, "run", side_effect=subprocess.TimeoutExpired("nmcli", 12)):
            failure = runtime.probe(["nmcli"])
        self.assertNotEqual(failure.returncode, 0)
        self.assertIn("TimeoutExpired", failure.stderr)

    def test_openbox_config_exposes_recovery_shortcuts(self):
        root = ET.parse(ROOT / "config/includes.chroot/etc/xdg/openbox/vaultboot-rc.xml")
        namespace = {"ob": "http://openbox.org/3.4/rc"}
        keys = {node.attrib["key"] for node in root.findall(".//ob:keybind", namespace)}
        self.assertTrue({"C-A-s", "C-A-t"}.issubset(keys))
        app = root.find(".//ob:application[@class='md.obsidian.Obsidian']", namespace)
        self.assertIsNotNone(app, "Match the WM_CLASS observed in the actual Obsidian 1.14.4 VM")
        self.assertEqual(app.find("ob:maximized", namespace).text, "yes")

    def test_redirects_require_official_https_hosts(self):
        runtime.check_url("https://release-assets.githubusercontent.com/example?signature=abc")
        for url in ["http://github.com/file", "https://github.com.evil.example/file", "https://example.com/file", "https://token@github.com/file", "https://github.com:444/file"]:
            with self.subTest(url=url), self.assertRaises(runtime.SetupError):
                runtime.check_url(url)


if __name__ == "__main__":
    unittest.main()
