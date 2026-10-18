#!/usr/bin/env python3
"""Vaultboot session, transactional upstream installer, and small system menu."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.parse
import urllib.request
import uuid


RELEASE_API = "https://api.github.com/repos/obsidianmd/obsidian-releases/releases/latest"
DOWNLOAD_PREFIX = "https://github.com/obsidianmd/obsidian-releases/releases/download/"
HTTPS_HOSTS = {"api.github.com", "github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com"}
MAX_DOWNLOAD = 1024 * 1024 * 1024
LOG = logging.getLogger("vaultboot")


class SetupError(Exception):
    """A failure that can be explained to the person at the screen."""


def run(args: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(args, check=False, **kwargs)


def dialog(kind: str, text: str, **options) -> subprocess.CompletedProcess:
    args = ["zenity", "--" + kind, "--title=Vaultboot", "--width=540", "--no-markup", "--text=" + text]
    for key, value in options.items():
        args.append("--" + key.replace("_", "-") + "=" + str(value))
    return run(args, capture_output=True, text=True)


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def check_url(url: str) -> None:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname not in HTTPS_HOSTS or parsed.port not in (None, 443) or parsed.username or parsed.password:
        raise SetupError("The download redirected outside the official HTTPS download service. Nothing was installed.")


def network_open(url: str):
    check_url(url)
    request = urllib.request.Request(url, headers={"User-Agent": "Vaultboot/0.1", "Accept": "application/vnd.github+json" if url == RELEASE_API else "application/octet-stream"})
    return urllib.request.build_opener(SafeRedirect()).open(request, timeout=45)


def select_asset(release: dict) -> dict:
    version = release.get("tag_name", "")
    if not re.fullmatch(r"v?[0-9]+(?:\.[0-9]+){1,3}(?:[-._][A-Za-z0-9]+)*", version):
        raise SetupError("The official release has an unexpected version. Nothing was installed.")
    for asset in release.get("assets", []):
        name = asset.get("name", "")
        if not re.fullmatch(r"obsidian_[0-9A-Za-z.+_-]+_amd64\.deb", name, flags=re.IGNORECASE):
            continue
        digest = asset.get("digest") or ""
        url = asset.get("browser_download_url", "")
        size = asset.get("size", 0)
        expected_url = DOWNLOAD_PREFIX + urllib.parse.quote(version, safe="") + "/" + urllib.parse.quote(name, safe="")
        if url != expected_url or not re.fullmatch(r"sha256:[a-fA-F0-9]{64}", digest) or not isinstance(size, int) or not 1 <= size <= MAX_DOWNLOAD:
            raise SetupError("This release does not provide a supported download with a SHA-256 digest. Nothing was installed.")
        return {"version": version, "name": name, "url": url, "size": size, "sha256": digest.split(":", 1)[1].lower()}
    raise SetupError("No 64-bit Intel/AMD Debian package was found in the official release.")


class Runtime:
    def __init__(self, home: Path | None = None):
        self.home = home or Path.home()
        self.data = self.home / ".local/share/vaultboot"
        self.state = self.home / ".local/state/vaultboot"
        self.releases = self.data / "releases"
        self.current = self.data / "current"
        self.notes = self.home / "Notes"
        self.child = None
        self.restart_requested = False
        self.stopping = False
        for folder in (self.data, self.state, self.releases):
            folder.mkdir(parents=True, exist_ok=True, mode=0o700)

    def configure_logging(self):
        handler = RotatingFileHandler(self.state / "session.log", maxBytes=2_000_000, backupCount=2)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        LOG.addHandler(handler)
        LOG.setLevel(logging.INFO)

    def executable(self) -> Path | None:
        target = self.current / "opt/Obsidian/obsidian"
        return target if target.is_file() and os.access(target, os.X_OK) else None

    def is_persistent(self) -> bool:
        """Follow the actual home mount and overlay upperdir to its backing disk."""
        result = run(["findmnt", "--noheadings", "--output", "FSTYPE,OPTIONS", "--target", str(self.home)], capture_output=True, text=True)
        parts = result.stdout.strip().split(None, 1)
        if result.returncode or not parts:
            return False
        if parts[0] not in {"overlay", "overlayfs"}:
            return parts[0] not in {"tmpfs", "ramfs", "squashfs", "rootfs", "iso9660"}
        options = dict(item.split("=", 1) for item in parts[1].split(",") if "=" in item) if len(parts) > 1 else {}
        upper = options.get("upperdir")
        if not upper:
            return False
        result = run(["findmnt", "--noheadings", "--output", "FSTYPE", "--target", str(Path(upper).resolve())], capture_output=True, text=True)
        return result.returncode == 0 and result.stdout.strip() not in {"", "tmpfs", "ramfs", "squashfs", "rootfs", "iso9660", "overlay", "overlayfs"}

    def confirm_storage(self) -> bool:
        if self.is_persistent():
            return True
        ack = self.state / "volatile-accepted"
        if ack.exists():
            return True
        result = dialog("question", "This is a temporary live session. Notes, settings, and the downloaded Obsidian app will disappear when you shut down.\n\nFor everyday writing, use the persistent Vaultboot USB image described in the download instructions. You can try this session now; copy any notes to another drive before shutting down.", ok_label="Try temporary session", cancel_label="Power off")
        if result.returncode:
            run(["systemctl", "poweroff"])
            return False
        ack.write_text("This temporary session was explicitly accepted.\n")
        return True

    def prepare_notes(self):
        self.notes.mkdir(exist_ok=True)
        (self.notes / ".obsidian").mkdir(exist_ok=True)
        note = self.notes / "Welcome to Vaultboot.md"
        if not note.exists():
            note.write_text("""# Your writing space

This is a local Obsidian vault. Your notes are ordinary Markdown files in `~/Notes`.

- **Ctrl+N** — new note.
- **Ctrl+O** — find and open a note.
- **Ctrl+P** — Obsidian command palette.
- **Ctrl+Alt+S** — Vaultboot system menu: Wi-Fi, backups, updates, and power.
- **Ctrl+Alt+T** — maintenance terminal.
- **Alt+F4** — close Obsidian; Vaultboot opens it again automatically.

## Keep your notes safe

Use the persistent USB image for everyday work. The ISO is a temporary demo unless you explicitly configured Debian persistence. The system menu shows whether your current session has persistent storage. A USB drive can fail: keep a second copy of your notes on another device.

The system menu can save a compressed backup of this Notes vault. If you create other vaults, back those up separately. Obsidian Sync and community plugins are optional features you configure in Obsidian itself.

## A small beginning

Create a note about something you would like to remember. Link notes by typing `[[` and choosing a title.

Vaultboot is an independent Debian-based project, not an official Obsidian product. Obsidian remains proprietary software supplied directly by its publisher.
""")
        config = self.home / ".config/obsidian/obsidian.json"
        if not config.exists():
            config.parent.mkdir(parents=True, exist_ok=True)
            config.write_text(json.dumps({"vaults": {uuid.uuid4().hex[:16]: {"path": str(self.notes), "ts": int(time.time() * 1000), "open": True}}}) + "\n")

    def check_sandbox(self):
        if os.geteuid() == 0:
            raise SetupError("Obsidian must run as the normal writer account, never as root.")
        probe = run(["unshare", "--user", "--map-root-user", "true"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if probe.returncode:
            raise SetupError("Linux user namespaces are unavailable, so Obsidian cannot start with its sandbox. Boot the supported Vaultboot image with its default kernel settings. See the maintenance terminal and session log for diagnostics.")

    def install(self, opener=network_open) -> dict:
        """Download/check/extract before atomically changing the launch target."""
        if os.geteuid() == 0:
            raise SetupError("Download Obsidian as the normal writer account, never as root.")
        with (self.state / "install.lock").open("a") as lock:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise SetupError("Another Obsidian download is already running.") from exc
            with opener(RELEASE_API) as response:
                metadata = response.read(2_000_001)
            if len(metadata) > 2_000_000:
                raise SetupError("The release response is unexpectedly large.")
            asset = select_asset(json.loads(metadata))
            existing = self.current / "release.json"
            if self.executable() and existing.is_file():
                try:
                    current = json.loads(existing.read_text())
                except (OSError, ValueError):
                    current = {}
                if current.get("sha256") == asset["sha256"]:
                    return asset
            needed = asset["size"] * 4 + 300 * 1024 * 1024
            if shutil.disk_usage(self.releases).free < needed:
                raise SetupError("There is not enough writable space for a safe download and extraction. Use a larger persistent USB image or free some space, then try again.")
            previous = self.current.resolve() if self.current.is_symlink() else None
            with tempfile.TemporaryDirectory(prefix=".install-", dir=self.releases) as scratch:
                scratch = Path(scratch)
                archive = scratch / "obsidian.deb"
                digest = hashlib.sha256()
                received = 0
                with opener(asset["url"]) as response, archive.open("wb") as output:
                    while chunk := response.read(1024 * 1024):
                        received += len(chunk)
                        if received > asset["size"] or received > MAX_DOWNLOAD:
                            raise SetupError("The download is larger than its published size. Nothing was installed.")
                        digest.update(chunk)
                        output.write(chunk)
                if received != asset["size"] or digest.hexdigest() != asset["sha256"]:
                    raise SetupError("The Obsidian download did not match its published SHA-256 checksum. Nothing was installed. Please retry the download.")
                extracted = scratch / "extracted"
                result = run(["dpkg-deb", "--extract", str(archive), str(extracted)], capture_output=True, text=True)
                if result.returncode:
                    raise SetupError("The verified package could not be extracted. Your previous version was kept.")
                executable = extracted / "opt/Obsidian/obsidian"
                if not executable.is_file() or not os.access(executable, os.X_OK):
                    raise SetupError("The package layout has changed. Your previous version was kept.")
                sandbox = extracted / "opt/Obsidian/chrome-sandbox"
                if sandbox.exists():
                    sandbox.chmod(0o755)
                (extracted / "release.json").write_text(json.dumps(asset, indent=2) + "\n")
                destination = self.releases / (asset["version"] + "-" + asset["sha256"][:16] + "-" + uuid.uuid4().hex[:8])
                extracted.rename(destination)
                link = self.data / (".current-" + uuid.uuid4().hex)
                try:
                    link.symlink_to(destination.relative_to(self.data), target_is_directory=True)
                    os.replace(link, self.current)
                finally:
                    link.unlink(missing_ok=True)
            self.prune_releases(previous)
            LOG.info("Installed verified official Obsidian %s", asset["version"])
            return asset

    def prune_releases(self, previous: Path | None):
        """Keep the newly installed release, its predecessor, and any running one."""
        protected = {self.current.resolve()}
        if previous:
            protected.add(previous.resolve())
        for entry in Path("/proc").glob("[0-9]*/exe"):
            try:
                relative = entry.resolve(strict=True).relative_to(self.releases)
                protected.add(self.releases / relative.parts[0])
            except (OSError, ValueError):
                continue
        for candidate in self.releases.iterdir():
            if candidate.is_symlink() or not candidate.is_dir() or candidate in protected:
                continue
            if not re.fullmatch(r"v?[0-9]+(?:\.[0-9]+){1,3}(?:[-._][A-Za-z0-9]+)*-[a-f0-9]{16}-[a-f0-9]{8}", candidate.name):
                continue
            if not (candidate / "release.json").is_file():
                continue
            try:
                shutil.rmtree(candidate)
            except OSError:
                LOG.warning("Could not remove an older cached release: %s", candidate.name)

    def download_ui(self) -> bool:
        progress = subprocess.Popen(["zenity", "--progress", "--pulsate", "--auto-close", "--no-cancel", "--title=Vaultboot", "--text=Downloading and verifying Obsidian from its publisher.\nThis can take a few minutes.", "--width=520"], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            asset = self.install()
        except Exception as exc:
            LOG.exception("Obsidian installation failed")
            message = str(exc) if isinstance(exc, SetupError) else "The download could not finish. Check your internet connection and try again. Your previous version, if any, was kept. Details are in the session log."
            progress.terminate()
            progress.wait()
            dialog("error", message)
            return False
        finally:
            if progress.poll() is None:
                progress.terminate()
                progress.wait()
        LOG.info("Ready to run %s", asset["version"])
        return True

    def setup(self) -> bool:
        while not self.executable():
            result = run(["zenity", "--list", "--title=Set up Vaultboot", "--width=600", "--height=400", "--text=Download Obsidian directly from its publisher. Internet is required once; later starts work offline.\n\nObsidian is proprietary software. By choosing Download, you agree to https://obsidian.md/terms\nPrivacy: https://obsidian.md/privacy", "--column=Action", "Download Obsidian", "Connect Wi-Fi / network", "Help / status", "Power off"], capture_output=True, text=True)
            choice = result.stdout.strip()
            if result.returncode:
                continue
            if choice == "Download Obsidian":
                if self.download_ui():
                    break
            elif choice == "Connect Wi-Fi / network":
                run(["xterm", "-title", "Network settings", "-e", "nmtui"])
            elif choice == "Help / status":
                self.status()
            elif choice == "Power off":
                if self.power("poweroff"):
                    return False
        return True

    def stop_child(self, *_):
        self.stopping = True
        if self.child and self.child.poll() is None:
            self.child.terminate()

    def restart_child(self, *_):
        self.restart_requested = True
        if self.child and self.child.poll() is None:
            self.child.terminate()

    def session(self) -> int:
        with (self.state / "session.lock").open("a") as lock:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return 0
            pidfile = self.state / "session.pid"
            pidfile.write_text(str(os.getpid()))
            signal.signal(signal.SIGTERM, self.stop_child)
            signal.signal(signal.SIGINT, self.stop_child)
            signal.signal(signal.SIGUSR1, self.restart_child)
            try:
                self.check_sandbox()
                if not self.confirm_storage():
                    return 0
                self.prepare_notes()
                if not self.setup():
                    return 0
                failures = 0
                while not self.stopping:
                    executable = self.executable()
                    if not executable:
                        if not self.setup():
                            return 1
                        continue
                    began = time.monotonic()
                    self.restart_requested = False
                    with (self.state / "obsidian.log").open("ab") as output:
                        self.child = subprocess.Popen([str(executable)], stdout=output, stderr=subprocess.STDOUT)
                        code = self.child.wait()
                    self.child = None
                    if self.stopping:
                        break
                    elapsed = time.monotonic() - began
                    failures = 0 if elapsed >= 60 or self.restart_requested else failures + 1
                    LOG.info("Obsidian exited code=%s after %.1fs; consecutive short runs=%s", code, elapsed, failures)
                    self.rotate_app_log()
                    if failures >= 5:
                        action = run(["zenity", "--list", "--title=Obsidian needs attention", "--width=560", "--height=320", "--text=Obsidian has stopped several times in a row. Your notes are kept. Choose an action; detailed output is in ~/.local/state/vaultboot/obsidian.log.", "--column=Action", "Try again", "Start without community plugins", "System menu", "Maintenance terminal", "Power off"], capture_output=True, text=True)
                        choice = action.stdout.strip()
                        if choice == "Start without community plugins":
                            self.disable_plugins()
                        elif choice == "System menu":
                            self.menu()
                        elif choice == "Maintenance terminal":
                            run(["xterm", "-title", "Vaultboot maintenance"])
                        elif choice == "Power off":
                            self.power("poweroff")
                        failures = 0
                    delay = min(30, 2 ** min(failures, 5)) if not self.restart_requested else 0
                    deadline = time.monotonic() + delay
                    while time.monotonic() < deadline and not self.stopping and not self.restart_requested:
                        time.sleep(0.25)
            except Exception as exc:
                LOG.exception("Session stopped")
                dialog("error", str(exc) if isinstance(exc, SetupError) else "Vaultboot could not start Obsidian. Press Ctrl+Alt+S for the system menu or Ctrl+Alt+T for a terminal. Details: ~/.local/state/vaultboot/session.log")
                return 1
            finally:
                pidfile.unlink(missing_ok=True)
        return 0

    def rotate_app_log(self):
        log = self.state / "obsidian.log"
        if log.exists() and log.stat().st_size > 4_000_000:
            os.replace(log, self.state / "obsidian.log.1")

    def disable_plugins(self):
        plugin_file = self.notes / ".obsidian/community-plugins.json"
        if plugin_file.exists():
            backup = plugin_file.with_name("community-plugins.backup-" + time.strftime("%Y%m%d-%H%M%S") + ".json")
            shutil.copy2(plugin_file, backup)
            plugin_file.write_text("[]\n")
        dialog("info", "Community plugins are disabled for the default Notes vault. Its previous plugin list was saved beside the original file. If another vault is failing, use the terminal to repair that vault's plugin settings.")

    def request_restart(self):
        try:
            pid = int((self.state / "session.pid").read_text())
            if pid <= 1:
                raise ValueError("invalid supervisor PID")
            command = Path(f"/proc/{pid}/cmdline").read_bytes()
            if b"vaultboot" not in command or b"session" not in command:
                raise ValueError("not a Vaultboot supervisor")
            os.kill(pid, signal.SIGUSR1)
        except (OSError, ValueError):
            dialog("info", "No active Obsidian supervisor was found. Restart the computer to open the installed app.")

    def power(self, action: str) -> bool:
        if dialog("question", "Save your work before " + ("shutting down." if action == "poweroff" else "restarting."), ok_label="Shut down" if action == "poweroff" else "Restart", cancel_label="Cancel").returncode == 0:
            result = run(["systemctl", action], capture_output=True, text=True)
            if result.returncode:
                dialog("error", "The power action was denied. Use the computer's physical power button or check the maintenance terminal.")
            return result.returncode == 0
        return False

    def status(self):
        persistence = "Persistent storage detected. Notes survive a normal shutdown." if self.is_persistent() else "TEMPORARY SESSION: notes and downloads disappear when you shut down."
        version = "not installed"
        try:
            version = json.loads((self.current / "release.json").read_text())["version"]
        except (OSError, ValueError, KeyError):
            pass
        dialog("info", "Vaultboot — your writing space\n\n" + persistence + "\n\nObsidian: " + version + "\nDefault vault: " + str(self.notes) + "\nLogs: " + str(self.state) + "\n\nCtrl+Alt+S: system menu\nCtrl+Alt+T: maintenance terminal\nCtrl+P: Obsidian command palette\n\nBack up your notes to another device regularly. Vaultboot is an independent project; Obsidian is supplied by its publisher.")

    def backup(self):
        result = run(["zenity", "--file-selection", "--directory", "--title=Choose a backup folder on another drive"], capture_output=True, text=True)
        if result.returncode or not result.stdout.strip():
            return
        destination = Path(result.stdout.strip()).resolve()
        if destination == self.notes.resolve() or self.notes.resolve() in destination.parents:
            dialog("error", "Choose a destination outside the Notes vault, preferably on a separate drive.")
            return
        archive = destination / ("vaultboot-notes-" + time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6] + ".tar.gz")
        partial = archive.with_suffix(".part")
        try:
            with tarfile.open(partial, "w:gz", dereference=False) as output:
                output.add(self.notes, arcname="Notes")
            os.replace(partial, archive)
        except (OSError, tarfile.TarError):
            LOG.exception("Backup failed")
            partial.unlink(missing_ok=True)
            dialog("error", "The backup could not be saved. Check that the destination is writable and has free space.")
            return
        dialog("info", "Saved the default Notes vault to:\n" + str(archive) + "\n\nOther vaults are not included. Keep this backup on a separate device.")

    def menu(self):
        with (self.state / "menu.lock").open("a") as lock:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
            result = run(["zenity", "--list", "--title=Vaultboot", "--width=430", "--height=460", "--text=System controls", "--column=Action", "Wi-Fi / network", "Sound volume", "Backup Notes vault", "Update Obsidian", "Restart Obsidian", "Help / storage status", "Maintenance terminal", "Restart computer", "Shut down"], capture_output=True, text=True)
            if result.returncode:
                return
            choice = result.stdout.strip()
            if choice == "Wi-Fi / network":
                run(["xterm", "-title", "Network settings", "-e", "nmtui"])
            elif choice == "Sound volume":
                run(["xterm", "-title", "Sound volume (M toggles mute)", "-e", "alsamixer"])
            elif choice == "Backup Notes vault":
                self.backup()
            elif choice == "Update Obsidian":
                if self.download_ui():
                    self.request_restart()
            elif choice == "Restart Obsidian":
                self.request_restart()
            elif choice == "Help / storage status":
                self.status()
            elif choice == "Maintenance terminal":
                subprocess.Popen(["xterm", "-title", "Vaultboot maintenance"], start_new_session=True)
            elif choice == "Restart computer":
                self.power("reboot")
            elif choice == "Shut down":
                self.power("poweroff")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["session", "menu", "update", "status"])
    args = parser.parse_args()
    runtime = Runtime()
    runtime.configure_logging()
    if args.action == "session":
        return runtime.session()
    if args.action == "menu":
        runtime.menu()
    elif args.action == "status":
        runtime.status()
    elif runtime.download_ui():
        runtime.request_restart()
    else:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
