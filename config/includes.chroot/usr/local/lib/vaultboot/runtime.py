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
import platform
import posixpath
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import textwrap
import time
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET


RELEASE_API = "https://api.github.com/repos/obsidianmd/obsidian-releases/releases/latest"
DOWNLOAD_PREFIX = "https://github.com/obsidianmd/obsidian-releases/releases/download/"
HTTPS_HOSTS = {"api.github.com", "github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com"}
MAX_DOWNLOAD = 1024 * 1024 * 1024
LOG = logging.getLogger("vaultboot")
LIVE_SYSTEM = Path("/run/live/medium/live/filesystem.squashfs")
POWER_SUPPLIES = Path("/sys/class/power_supply")
KEYBOARD_RULES = Path("/usr/share/X11/xkb/rules/base.xml")


class SetupError(Exception):
    """A failure that can be explained to the person at the screen."""


def run(args: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(args, check=False, **kwargs)


def dialog(kind: str, text: str, **options) -> subprocess.CompletedProcess:
    args = ["zenity", "--" + kind, "--title=Vaultboot", "--width=540", "--no-markup", "--text=" + text]
    for key, value in options.items():
        args.append("--" + key.replace("_", "-") + "=" + str(value))
    return run(args, capture_output=True, text=True)


def probe(args: list[str], timeout: int = 12) -> subprocess.CompletedProcess:
    """Bounded system query; missing hardware/services should not close the menu."""
    try:
        return run(args, capture_output=True, text=True, timeout=timeout,
                   env={**os.environ, "LC_ALL": "C"})
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(args, 1, "", type(exc).__name__)


def display_outputs(text: str) -> dict:
    outputs = {}
    current = None
    for line in text.splitlines():
        header = re.match(r"^(\S+) (connected|disconnected)\b", line)
        if header:
            current = header[1] if header[2] == "connected" else None
            if current:
                outputs[current] = {"modes": [], "current": None}
        elif current:
            mode = re.match(r"^\s+(\d+x\d+\S*)\s+(.+)$", line)
            if mode:
                outputs[current]["modes"].append(mode[1])
                if "*" in mode[2]:
                    outputs[current]["current"] = mode[1]
    return outputs


def battery_status() -> str:
    details = []
    try:
        devices = list(POWER_SUPPLIES.iterdir())
    except OSError:
        devices = []
    for device in devices:
        try:
            kind = (device / "type").read_text().strip()
            if kind == "Battery":
                capacity = int((device / "capacity").read_text())
                state = (device / "status").read_text().strip()
                details.append(f"Battery: {capacity}% ({state})")
            elif kind in {"Mains", "USB", "USB_C"}:
                details.append("External power: " + ("connected" if (device / "online").read_text().strip() == "1" else "disconnected"))
        except (OSError, ValueError):
            continue
    return "\n".join(details) or "Battery: not reported by this computer"


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


def debian_architecture() -> str:
    machine = platform.machine().lower()
    if machine in {"x86_64", "amd64"}:
        return "amd64"
    if machine in {"aarch64", "arm64"}:
        return "arm64"
    raise SetupError("Obsidian requires a supported 64-bit Intel/AMD or ARM Linux system. This operating system architecture is unsupported.")


def select_asset(release: dict, architecture: str = "amd64") -> dict:
    if architecture not in {"amd64", "arm64"}:
        raise SetupError("This operating system architecture is unsupported.")
    version = release.get("tag_name", "")
    if not re.fullmatch(r"v?[0-9]+(?:\.[0-9]+){1,3}(?:[-._][A-Za-z0-9]+)*", version):
        raise SetupError("The official release has an unexpected version. Nothing was installed.")
    for asset in release.get("assets", []):
        name = asset.get("name", "")
        package_format = "deb"
        if re.fullmatch(r"obsidian_[0-9A-Za-z.+_-]+_" + architecture + r"\.deb", name, flags=re.IGNORECASE):
            pass
        elif architecture == "arm64" and re.fullmatch(r"obsidian-[0-9A-Za-z.+_-]+-arm64\.tar\.gz", name, flags=re.IGNORECASE):
            package_format = "tar"
        else:
            continue
        digest = asset.get("digest") or ""
        url = asset.get("browser_download_url", "")
        size = asset.get("size", 0)
        expected_url = DOWNLOAD_PREFIX + urllib.parse.quote(version, safe="") + "/" + urllib.parse.quote(name, safe="")
        if url != expected_url or not re.fullmatch(r"sha256:[a-fA-F0-9]{64}", digest) or not isinstance(size, int) or not 1 <= size <= MAX_DOWNLOAD:
            raise SetupError("This release does not provide a supported download with a SHA-256 digest. Nothing was installed.")
        return {"version": version, "architecture": architecture, "format": package_format, "name": name, "url": url, "size": size, "sha256": digest.split(":", 1)[1].lower()}
    raise SetupError("No supported " + architecture + " Linux package was found in the official release.")


def extract_tar(archive: Path, destination: Path):
    """Extract upstream ARM64 archives without paths or links escaping staging."""
    if not hasattr(tarfile, "data_filter"):
        raise SetupError("Safe archive extraction requires a newer Python version. Update the operating system first.")
    payload = destination.parent / "tar-payload"
    payload.mkdir()
    try:
        with tarfile.open(archive, "r:gz") as source:
            members = []
            expanded = 0
            for member in source:
                expanded += member.size
                if len(members) >= 50_000 or expanded > 2 * 1024**3:
                    raise SetupError("The archive exceeds safe extraction limits.")
                name = member.name
                if name.startswith("/") or ".." in name.split("/"):
                    raise SetupError("The archive contains an unsafe path. Nothing was installed.")
                if not (member.isfile() or member.isdir() or member.issym() or member.islnk()):
                    raise SetupError("The archive contains a special file. Nothing was installed.")
                if member.issym() or member.islnk():
                    target = member.linkname
                    resolved = posixpath.normpath(posixpath.join(posixpath.dirname(name) if member.issym() else "", target))
                    if target.startswith("/") or resolved == ".." or resolved.startswith("../"):
                        raise SetupError("The archive contains an escaping link. Nothing was installed.")
                members.append(member)
            # The data filter additionally resolves existing symlinks on disk for
            # each member and strips privileged ownership/mode metadata.
            source.extractall(payload, members=members, filter="data")
    except (tarfile.TarError, OSError, ValueError) as exc:
        raise SetupError("The verified archive could not be safely extracted. Your previous version was kept.") from exc
    candidates = [folder for folder in [payload, *payload.iterdir()]
                  if folder.is_dir() and not folder.is_symlink()
                  and (folder / "obsidian").is_file()
                  and not (folder / "obsidian").is_symlink()]
    if len(candidates) != 1:
        raise SetupError("The ARM64 archive layout has changed. Your previous version was kept.")
    target = destination / "opt/Obsidian"
    target.parent.mkdir(parents=True)
    candidates[0].rename(target)


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

    def settings(self) -> dict:
        try:
            value = json.loads((self.data / "settings.json").read_text())
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    def save_setting(self, key: str, value):
        with (self.data / "settings.lock").open("a") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            settings = self.settings()
            settings[key] = value
            temporary = self.data / ".settings.tmp"
            temporary.write_text(json.dumps(settings, indent=2) + "\n")
            os.replace(temporary, self.data / "settings.json")

    def apply_settings(self):
        settings = self.settings()
        keyboard = settings.get("keyboard", {})
        if isinstance(keyboard, dict) and isinstance(keyboard.get("layout"), str) and isinstance(keyboard.get("variant", ""), str) and re.fullmatch(r"[a-z][a-z0-9_-]*", keyboard["layout"]) and re.fullmatch(r"(?:[a-z0-9][a-z0-9_-]*)?", keyboard.get("variant", "")):
            if probe(["setxkbmap", "-layout", keyboard["layout"], "-variant", keyboard.get("variant", "")]).returncode:
                LOG.warning("Saved keyboard layout could not be applied")
        display = settings.get("display", {})
        if isinstance(display, dict) and display:
            outputs = display_outputs(probe(["xrandr", "--query"]).stdout)
            output, mode = display.get("output"), display.get("mode")
            if isinstance(output, str) and output in outputs and mode in outputs[output]["modes"]:
                if probe(["xrandr", "--output", output, "--mode", mode]).returncode:
                    LOG.warning("Saved display resolution could not be applied")
        brightness = settings.get("brightness")
        if isinstance(brightness, int) and 5 <= brightness <= 100:
            probe(["sudo", "-n", "brightnessctl", "-c", "backlight", "set", str(brightness) + "%"])

    def choose(self, title: str, text: str, choices: list[str], height: int = 440) -> str:
        # Zenity's list label otherwise makes long prose a minimum window
        # width, even when --width is given; keep setup usable at 800x600.
        text = "\n".join(textwrap.fill(line, width=76, break_long_words=False, break_on_hyphens=False)
                         for line in text.split("\n"))
        selected = run(["zenity", "--list", "--title=" + title, "--width=600", "--height=" + str(height), "--no-markup", "--text=" + text, "--column=Action", *choices], capture_output=True, text=True)
        value = selected.stdout.strip()
        return value if not selected.returncode and value in choices else ""

    def checked(self, args: list[str], message: str) -> bool:
        result = probe(args)
        if result.returncode:
            dialog("error", message + "\n\n" + result.stderr.strip()[:400])
            return False
        return True

    def launch(self, args: list[str]):
        try:
            subprocess.Popen(args, start_new_session=True)
        except OSError:
            LOG.exception("Could not open %s", args[0])
            dialog("error", "Could not open " + args[0] + ". Use the maintenance terminal to check the installed packages.")

    def network_summary(self) -> str:
        result = probe(["nmcli", "--colors", "no", "-t", "-f", "DEVICE,TYPE,STATE", "device", "status"])
        if result.returncode:
            return "Network status unavailable. Check NetworkManager in the maintenance terminal."
        rows = [line for line in result.stdout.splitlines() if ":loopback:" not in line]
        return "Network devices (device:type:state):\n" + ("\n".join(rows) or "No network adapter detected; try Ethernet or a supported USB Wi-Fi adapter.")

    def network(self):
        choice = self.choose("Wi-Fi / Ethernet", self.network_summary() + "\n\nChoose Connect, then use arrows to select a network, Tab to move, and Enter to connect. Enter its password when asked. Back returns here. Saved connections normally reconnect on later boots when storage is persistent.\n\nA web sign-in network needs a browser; use home Wi-Fi, Ethernet, or a phone hotspot for setup.", ["Connect to Wi-Fi / Ethernet", "Turn Wi-Fi on", "Edit saved connections", "Refresh status"], height=500)
        if choice == "Turn Wi-Fi on":
            self.checked(["nmcli", "radio", "wifi", "on"], "Wi-Fi could not be enabled. Check the hardware wireless switch or airplane-mode key.")
        elif choice in {"Connect to Wi-Fi / Ethernet", "Edit saved connections"}:
            command = "nmtui-connect" if choice == "Connect to Wi-Fi / Ethernet" else "nmtui-edit"
            result = run(["xterm", "-geometry", "90x28", "-title", "Wi-Fi / Ethernet — arrows, Tab, Enter; Back to finish", "-e", command])
            if result.returncode:
                dialog("error", "Network settings could not open. NetworkManager may be unavailable. Try Ethernet or inspect the maintenance terminal.")
        if choice:
            dialog("info", self.network_summary() + "\n\nA connected adapter does not guarantee internet access. If an Obsidian download fails, check this connection and retry; an installed app works offline.\n\nIf Wi-Fi is missing, its driver or firmware may not support this adapter. Ethernet or a supported USB adapter can provide a connection.")

    def keyboard(self):
        try:
            layouts = ET.parse(KEYBOARD_RULES).getroot().findall("./layoutList/layout")
        except (OSError, ET.ParseError):
            dialog("error", "Keyboard layout data is unavailable. Check the xkb-data package in the maintenance terminal.")
            return
        options = {}
        for layout in layouts:
            code = layout.findtext("./configItem/name", "")
            label = layout.findtext("./configItem/description", code)
            if re.fullmatch(r"[a-z][a-z0-9_-]*", code):
                options[label + " (" + code + ")"] = (code, layout)
        choice = self.choose("Keyboard layout", "Choose your keyboard before typing a Wi-Fi password. Type the beginning of a language name to find it.", sorted(options))
        if not choice:
            return
        code, layout = options[choice]
        variants = {"Standard": ""}
        for item in layout.findall("./variantList/variant"):
            name = item.findtext("./configItem/name", "")
            if re.fullmatch(r"[a-z0-9_-]+", name):
                variants[item.findtext("./configItem/description", name)] = name
        variant_choice = self.choose("Keyboard variant", "Choose Standard unless your keyboard needs one of these variants.", list(variants)) if len(variants) > 1 else "Standard"
        if variant_choice and self.checked(["setxkbmap", "-layout", code, "-variant", variants[variant_choice]], "The keyboard layout could not be changed."):
            self.save_setting("keyboard", {"layout": code, "variant": variants[variant_choice]})
            dialog("info", "Keyboard layout applied to the graphical session and saved for later boots on persistent storage. Text-console layouts can be changed with sudo dpkg-reconfigure keyboard-configuration.")

    def display(self):
        result = probe(["xrandr", "--query"])
        outputs = display_outputs(result.stdout)
        if result.returncode or not outputs:
            dialog("info", "This display does not expose changeable resolutions. Use the monitor's controls or check the graphics driver in the maintenance terminal.")
            return
        output = self.choose("Display", "Choose a connected display. Its resolution will revert after 15 seconds unless you keep it.", list(outputs))
        if not output:
            return
        previous = outputs[output]["current"]
        mode = self.choose("Display resolution", "Current: " + (previous or "disabled") + ". Choose a supported resolution.", outputs[output]["modes"])
        if not mode or mode == previous:
            return
        if not self.checked(["xrandr", "--output", output, "--mode", mode], "The display could not use that resolution; the saved setting was kept."):
            return
        keep = False
        try:
            keep = dialog("question", "Keep this resolution? It will revert automatically in 15 seconds.", timeout=15, ok_label="Keep resolution", cancel_label="Revert").returncode == 0
        finally:
            if not keep:
                restore = ["--mode", previous] if previous else ["--off"]
                if probe(["xrandr", "--output", output, *restore]).returncode:
                    dialog("error", "The previous display resolution could not be restored. Press Ctrl+Alt+T and run xrandr --auto to ask the graphics driver for its preferred resolution, or restart the computer. The rejected setting was not saved.")
        if keep:
            self.save_setting("display", {"output": output, "mode": mode})

    def brightness(self):
        result = probe(["brightnessctl", "-c", "backlight", "-m", "info"])
        match = re.search(r",(\d+)%,", result.stdout)
        if result.returncode or not match:
            dialog("info", "This computer does not expose a laptop backlight control. Use the physical brightness buttons on an external monitor. Some laptop firmware needs a different graphics driver.")
            return
        choice = dialog("scale", "Screen brightness", value=max(5, min(100, int(match[1]))), min_value=5, max_value=100, step=5)
        try:
            value = int(choice.stdout.strip())
        except ValueError:
            return
        if not choice.returncode and 5 <= value <= 100 and self.checked(["sudo", "-n", "brightnessctl", "-c", "backlight", "set", str(value) + "%"], "Brightness could not be changed. The device may not allow this user to control it."):
            self.save_setting("brightness", value)

    def timezone(self):
        result = probe(["timedatectl", "list-timezones"])
        zones = result.stdout.splitlines()
        if result.returncode or not zones:
            dialog("error", "Time-zone information is unavailable. Check systemd and tzdata in the maintenance terminal.")
            return
        current = probe(["timedatectl", "show", "--property=Timezone", "--value"]).stdout.strip()
        zone = self.choose("Time zone", "Current: " + current + ". Choose Region/City; type the beginning of a region to find it. The clock synchronizes automatically when internet access is available.", zones)
        if zone and self.checked(["timedatectl", "--no-ask-password", "set-timezone", zone], "The time zone could not be changed."):
            if hasattr(time, "tzset"):
                time.tzset()
            dialog("info", "Time zone set to " + zone + ".\n" + time.strftime("%Y-%m-%d %H:%M %Z"))

    def sound(self):
        if shutil.which("pavucontrol"):
            self.launch(["pavucontrol"])
        else:
            self.launch(["xterm", "-title", "Sound: F6 chooses device, M toggles mute", "-e", "alsamixer"])

    def files(self):
        self.notes.mkdir(exist_ok=True)
        self.launch(["pcmanfm", str(self.notes)])

    def can_install(self) -> bool:
        return platform.machine().lower() in {"x86_64", "amd64"} and LIVE_SYSTEM.is_file()

    def install_disk(self):
        if not self.can_install():
            dialog("info", "Internal-drive installation is available only when booted from the PC Vaultboot live USB. The Raspberry Pi image belongs on its microSD card.")
            return
        self.launch(["xterm", "-hold", "-geometry", "100x32", "-title", "Install Vaultboot to an internal drive", "-e", "sudo", "/usr/local/sbin/vaultboot-install"])

    def system_updates(self):
        if dialog("question", "Back up your notes and connect to power and the internet before updating Debian packages. The terminal will show changes for you to review.\n\nOn a PC live image, package updates do not replace its boot kernel. Flash a newer Vaultboot release for base-system/kernel updates. Keep enough free space for downloads and installation.", ok_label="Open package updates", cancel_label="Cancel").returncode:
            return
        self.launch(["xterm", "-hold", "-geometry", "100x32", "-title", "Debian package updates — review before accepting", "-e", "sh", "-c", "sudo apt update && sudo apt upgrade"])

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
- **Ctrl+Alt+S** — system menu: Wi-Fi, files, sound, display, keyboard, backups, updates, and power.
- **Ctrl+Alt+F** — open your files and attached drives.
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
            asset = select_asset(json.loads(metadata), debian_architecture())
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
                archive = scratch / asset["name"]
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
                if asset["format"] == "tar":
                    extract_tar(archive, extracted)
                else:
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
            choices = ["Download Obsidian", "Wi-Fi / Ethernet", "Keyboard layout", "Time zone"]
            if self.can_install():
                choices.append("Install to internal drive")
            choices += ["Help / storage status", "Power off"]
            choice = self.choose("Set up Vaultboot", "Connect to the internet, then download Obsidian directly from its publisher. Internet is needed once; later starts work offline. Use Keyboard layout before entering a Wi-Fi password.\n\nObsidian is proprietary software. Choosing Download agrees to https://obsidian.md/terms\nPrivacy: https://obsidian.md/privacy", choices, height=540)
            if choice == "Download Obsidian":
                if self.download_ui():
                    break
            elif choice == "Wi-Fi / Ethernet":
                self.network()
            elif choice == "Keyboard layout":
                self.keyboard()
            elif choice == "Time zone":
                self.timezone()
            elif choice == "Install to internal drive":
                self.install_disk()
            elif choice == "Help / storage status":
                self.status()
            elif choice == "Power off":
                if self.power("poweroff"):
                    return False
        return True

    def stop_child(self, *_):
        self.stopping = True
        self.signal_child(signal.SIGTERM)

    def restart_child(self, *_):
        self.restart_requested = True
        self.signal_child(signal.SIGTERM)

    def signal_child(self, sig: int):
        if self.child and self.child.poll() is None:
            try:
                os.killpg(self.child.pid, sig)
            except ProcessLookupError:
                pass

    def wait_for_child(self) -> int:
        stop_deadline = None
        while True:
            try:
                return self.child.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                self.rotate_app_log()
                if self.stopping or self.restart_requested:
                    if stop_deadline is None:
                        stop_deadline = time.monotonic() + 8
                    elif time.monotonic() >= stop_deadline:
                        self.signal_child(signal.SIGKILL)

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
                self.apply_settings()
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
                        self.child = subprocess.Popen([str(executable)], stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
                        code = self.wait_for_child()
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
            # The live child holds this inode open with O_APPEND. Preserve a bounded
            # tail and truncate in place so a long-running app cannot fill storage.
            with log.open("rb") as source:
                source.seek(-4_000_000, os.SEEK_END)
                (self.state / "obsidian.log.1").write_bytes(source.read(4_000_000))
            with log.open("wb"):
                pass

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
        actions = {"poweroff": ("shutting down", "Shut down"), "reboot": ("restarting", "Restart"), "suspend": ("suspending", "Suspend")}
        if action not in actions:
            raise ValueError("Unsupported power action")
        verb, label = actions[action]
        text = "Save your work before " + verb + "."
        if action == "suspend":
            text += " Keep the boot USB connected. Resume support depends on this computer; test it before relying on it for unsaved work."
        if dialog("question", text, ok_label=label, cancel_label="Cancel").returncode == 0:
            result = run(["systemctl", action], capture_output=True, text=True)
            if result.returncode:
                dialog("error", "This power action is unavailable or was denied. Save your work and use Shut down, or check the maintenance terminal.")
            return result.returncode == 0
        return False

    def status(self):
        persistence = "Persistent storage detected. Notes survive a normal shutdown." if self.is_persistent() else "TEMPORARY SESSION: notes and downloads disappear when you shut down."
        version = "not installed"
        try:
            version = json.loads((self.current / "release.json").read_text())["version"]
        except (OSError, ValueError, KeyError):
            pass
        try:
            usage = shutil.disk_usage(self.home)
            space = f"Writable storage: {usage.free / 1024**3:.1f} GiB free of {usage.total / 1024**3:.1f} GiB"
        except OSError:
            space = "Writable storage: unavailable"
        text = "Vaultboot — your writing space\n\n" + persistence + "\n" + space + "\n\n" + battery_status() + "\n" + time.strftime("%Y-%m-%d %H:%M %Z") + "\n\n" + self.network_summary() + "\n\nObsidian: " + version + "\nDefault vault: " + str(self.notes) + "\nLogs: " + str(self.state) + "\n\nCtrl+Alt+S: system menu\nCtrl+Alt+F: files and attached drives\nCtrl+Alt+T: maintenance terminal\n\nBack up your notes to another device regularly. A connected network may still need internet access."
        run(["zenity", "--text-info", "--title=Storage, battery and system status", "--width=600", "--height=500", "--ok-label=Close"], input=text, capture_output=True, text=True)

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
            actions = {"Wi-Fi / Ethernet": self.network, "Files and attached drives": self.files,
                       "Sound / headphones": self.sound, "Display resolution": self.display,
                       "Screen brightness": self.brightness, "Keyboard layout": self.keyboard,
                       "Time zone": self.timezone, "Storage / battery / status": self.status,
                       "Backup Notes vault": self.backup, "Update Debian packages": self.system_updates}
            choices = list(actions) + ["Update Obsidian", "Restart Obsidian"]
            if self.can_install():
                choices.append("Install to internal drive")
                actions["Install to internal drive"] = self.install_disk
            choices += ["Maintenance terminal", "Suspend", "Restart computer", "Shut down"]
            choice = self.choose("Vaultboot system menu", battery_status() + "\nChoose a control; Esc returns to your notes.", choices, height=520)
            if choice in actions:
                actions[choice]()
            elif choice == "Update Obsidian":
                if self.download_ui():
                    self.request_restart()
            elif choice == "Restart Obsidian":
                self.request_restart()
            elif choice == "Maintenance terminal":
                self.launch(["xterm", "-title", "Vaultboot maintenance"])
            elif choice == "Suspend":
                self.power("suspend")
            elif choice == "Restart computer":
                self.power("reboot")
            elif choice == "Shut down":
                self.power("poweroff")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["session", "menu", "update", "status", "files"])
    args = parser.parse_args()
    runtime = Runtime()
    runtime.configure_logging()
    try:
        if args.action == "session":
            return runtime.session()
        if args.action == "menu":
            runtime.menu()
        elif args.action == "status":
            runtime.status()
        elif args.action == "files":
            runtime.files()
        elif runtime.download_ui():
            runtime.request_restart()
        else:
            return 1
    except (OSError, subprocess.SubprocessError, SetupError):
        LOG.exception("System control failed")
        dialog("error", "This system control could not finish. Check free storage and the session log in ~/.local/state/vaultboot. Ctrl+Alt+T opens a maintenance terminal.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
