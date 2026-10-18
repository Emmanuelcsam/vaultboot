#!/usr/bin/env python3
"""Guided removable-media flasher. Standard library only; no safety bypass flags.

Device adapters never accept regular files. Tests inject a separate, private
adapter rather than weakening the production device checks.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
try:
    import fcntl
except ImportError:  # Give Windows users the supported-platform message.
    fcntl = None
import gzip
import hashlib
import json
import os
from pathlib import Path
import platform
import plistlib
import re
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request

# This same module is shipped inside the OS. The standalone bundle preserves
# these paths so preparation and consumption share one format and pin policy.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / 'config/includes.chroot/usr/local/lib/vaultboot'))
import preload
PIN_MANIFEST = PROJECT_ROOT / 'config/includes.chroot/usr/share/vaultboot/obsidian-offline.json'

REPOSITORY = "Emmanuelcsam/vaultboot"
API = "https://api.github.com/repos/" + REPOSITORY
MIB = 1024 * 1024
GIB = 1024 * MIB
CHUNK = 4 * MIB
MAX_COMPRESSED = 8 * GIB
MAX_RAW = 32 * GIB
RESERVE = 128 * MIB
TAG = re.compile(r"v[0-9]+\.[0-9]+\.[0-9]+(?:[-.][A-Za-z0-9.-]+)?\Z")
IMAGE_NAME = re.compile(r"vaultboot-([0-9]+\.[0-9]+\.[0-9]+(?:[-.][A-Za-z0-9.-]+)?)-(amd64|pi3-arm64)\.img\.gz\Z")
TRUSTED_HOSTS = {"api.github.com", "github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com"}
SAFE_PATH = "/usr/sbin:/usr/bin:/sbin:/bin:/usr/local/bin"


class Refused(RuntimeError):
    """A safety check failed; do not continue to device writes."""


def run(*args):
    try:
        return subprocess.run(args, check=True, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=60,
                              env={**os.environ, "PATH": SAFE_PATH, "LC_ALL": "C"}).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        detail = getattr(exc, "stderr", b"").decode("utf-8", "replace").strip()
        raise Refused(f"Command failed: {args[0]} {args[1] if len(args) > 1 else ''}: {detail or exc}") from exc


def printable(value):
    return "".join(c if c.isprintable() else "?" for c in str(value or ""))


def valid_https(url):
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme != "https" or parsed.hostname not in TRUSTED_HOSTS
            or parsed.username or parsed.password or parsed.port not in (None, 443)):
        raise Refused("Download redirected outside the allowed GitHub HTTPS hosts")
    return url


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        valid_https(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def open_url(url):
    req = urllib.request.Request(valid_https(url), headers={
        "User-Agent": "Vaultboot-Autoflash", "Accept": "application/vnd.github+json" if url.startswith(API) else "application/octet-stream"})
    response = urllib.request.build_opener(SafeRedirect()).open(req, timeout=30)
    valid_https(response.geturl())
    return response


def fetch_small(url, maximum=2 * MIB):
    with open_url(url) as response:
        body = response.read(maximum + 1)
    if len(body) > maximum:
        raise Refused("Release metadata exceeds the size limit")
    return body


def assets_for(release, target):
    tag = release.get("tag_name", "")
    if release.get("draft") or not TAG.fullmatch(tag) or not release.get("published_at"):
        raise Refused("Release is not a published Vaultboot version")
    name = f"vaultboot-{tag[1:]}-{target}.img.gz"
    assets = release.get("assets", [])
    selected = []
    for wanted in (name, "SHA256SUMS"):
        matches = [a for a in assets if a.get("name") == wanted and a.get("state") == "uploaded"]
        if len(matches) != 1:
            raise Refused(f"Release {tag} does not have exactly one {wanted}")
        asset = matches[0]
        expected = f"https://github.com/{REPOSITORY}/releases/download/{tag}/{wanted}"
        if asset.get("browser_download_url") != expected:
            raise Refused("Unexpected release asset URL")
        selected.append(asset)
    return tag, selected[0], selected[1]


def discover_release(target, tag=None):
    if tag:
        if not TAG.fullmatch(tag):
            raise Refused("--release must be a version such as v0.1.0-preview.2")
        return assets_for(json.loads(fetch_small(API + "/releases/tags/" + tag)), target)
    # List releases includes prereleases. GitHub's /latest endpoint excludes them.
    releases = json.loads(fetch_small(API + "/releases?per_page=100"))
    if not isinstance(releases, list):
        raise Refused("Invalid release list")
    for release in sorted(releases, key=lambda r: r.get("published_at") or "", reverse=True):
        try:
            return assets_for(release, target)
        except Refused:
            continue
    raise Refused("No published release with this image and SHA256SUMS was found")


def manifest_hash(body, filename):
    if len(body) > MIB:
        raise Refused("Checksum manifest exceeds the size limit")
    try:
        lines = body.decode("ascii").splitlines()
    except UnicodeError as exc:
        raise Refused("Checksum manifest is not ASCII") from exc
    entries = {}
    for line in lines:
        match = re.fullmatch(r"([a-fA-F0-9]{64}) [ *]([^\x00-\x1f\x7f]+)", line)
        if not match:
            raise Refused("Malformed checksum manifest")
        digest, name = match.groups()
        if name in entries or name.startswith("/") or ".." in Path(name).parts:
            raise Refused("Duplicate or unsafe checksum filename")
        entries[name] = digest.lower()
    if filename not in entries:
        raise Refused("Image has no entry in SHA256SUMS")
    return entries[filename]


def space_for(directory, amount):
    if shutil.disk_usage(directory).free < amount + RESERVE:
        raise Refused("Not enough staging space; choose another --cache-dir (image plus compressed download needed)")


def copy_verified(source, destination, expected, expected_size=None):
    digest, total = hashlib.sha256(), 0
    with destination.open("xb") as output:
        while True:
            block = source.read(CHUNK)
            if not block:
                break
            total += len(block)
            if total > MAX_COMPRESSED or (expected_size is not None and total > expected_size):
                raise Refused("Compressed image exceeds its declared size or the 8 GiB limit")
            space_for(destination.parent, len(block))
            output.write(block)
            digest.update(block)
    if expected_size is not None and total != expected_size:
        raise Refused("Incomplete image download")
    if digest.hexdigest() != expected:
        raise Refused("Downloaded image SHA-256 does not match SHA256SUMS")


def decompress_verified(compressed, raw):
    digest, total = hashlib.sha256(), 0
    try:
        with gzip.open(compressed, "rb") as source, raw.open("xb") as output:
            while True:
                block = source.read(CHUNK)
                if not block:
                    break
                total += len(block)
                if total > MAX_RAW:
                    raise Refused("Uncompressed image exceeds the 32 GiB safety limit")
                space_for(raw.parent, len(block))
                output.write(block)
                digest.update(block)
            output.flush()
            os.fsync(output.fileno())
    except (gzip.BadGzipFile, EOFError, OSError) as exc:
        raise Refused(f"Image decompression failed before any device write: {exc}") from exc
    with raw.open("rb") as source:
        header = source.read(512)
    if total < MIB or total % 512 or header[510:512] != b"\x55\xaa":
        raise Refused("Image has an invalid disk size or boot-sector signature")
    return total, digest.hexdigest()


def regular_input(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise Refused("Local image and manifest must be regular files, not devices or symlinks")
    return os.fdopen(fd, "rb")


def stage_image(args, directory):
    compressed, raw = directory / "download.img.gz", directory / "verified.img"
    if args.image:
        name = Path(args.image).name
        match = IMAGE_NAME.fullmatch(name)
        if not match or match.group(2) != args.target:
            raise Refused("Local filename must be vaultboot-VERSION-TARGET.img.gz and match --target")
        if args.release and args.release != "v" + match.group(1):
            raise Refused("Local image version does not match --release")
        with regular_input(args.manifest) as source:
            expected = manifest_hash(source.read(MIB + 1), name)
        with regular_input(args.image) as source:
            size = os.fstat(source.fileno()).st_size
            if size > MAX_COMPRESSED:
                raise Refused("Compressed image exceeds the 8 GiB limit")
            space_for(directory, size)
            copy_verified(source, compressed, expected, size)
        tag = "v" + match.group(1)
    else:
        tag, asset, manifest = discover_release(args.target, args.release)
        name = asset["name"]
        size = asset.get("size")
        if not isinstance(size, int) or not 0 < size <= MAX_COMPRESSED:
            raise Refused("Invalid release image size")
        expected = manifest_hash(fetch_small(manifest["browser_download_url"], MIB), name)
        space_for(directory, size)
        print(f"Downloading {name} ({size / GIB:.2f} GiB compressed)...", flush=True)
        with open_url(asset["browser_download_url"]) as source:
            copy_verified(source, compressed, expected, size)
    print("Download checksum verified. Checking and staging the complete raw image...", flush=True)
    size, digest = decompress_verified(compressed, raw)
    compressed.unlink()
    print(f"Ready: {tag}, {args.target}, {size / GIB:.2f} GiB; raw SHA-256 {digest}")
    return raw, size, digest


def preload_image(args, raw, directory, accept=None):
    """Prepare a private regular image; all failures precede target selection."""
    asset = preload.asset_for(preload.load_manifest(PIN_MANIFEST),
                              'arm64' if args.target == 'pi3-arm64' else 'amd64')
    with regular_input(raw) as image:
        slot = preload.locate_slot(image)
        if slot is None:
            raise Refused('This image has no offline package partition. Use preview.3 or newer, '
                          'or --no-preload for an online first setup')
        if preload.read_slot(image, *slot, asset) is not None:
            raise Refused('The downloaded image unexpectedly already contains Obsidian')
    print(f'\nPrepare offline first boot with Obsidian {asset["version"]} ({asset["architecture"]}).')
    print('Obsidian is proprietary software downloaded directly from its publisher for this drive.')
    print('Terms: https://obsidian.md/terms  Privacy: https://obsidian.md/privacy')
    answer = (accept or input)('Type ACCEPT to accept these terms and prepare your offline copy: ').strip()
    if answer != 'ACCEPT':
        raise Refused('Obsidian terms were not accepted; no device was erased. '
                      'Use --no-preload if you prefer to download during first setup')
    package = directory / 'obsidian-package'
    space_for(directory, asset['size'])
    print(f'Downloading and verifying {asset["name"]} directly from Obsidian...', flush=True)
    with open_url(asset['url']) as source:
        copy_verified(source, package, asset['sha256'], asset['size'])
    with raw.open('r+b') as image, regular_input(package) as source:
        preload.write_slot(image, *slot, asset, source)
    package.unlink()
    digest = hashlib.sha256()
    with regular_input(raw) as image:
        while True:
            block = image.read(CHUNK)
            if not block:
                break
            digest.update(block)
    print('Offline Obsidian copy verified and included. First boot will not need internet.')
    print(f'Prepared raw SHA-256: {digest.hexdigest()}')
    return digest.hexdigest()


@dataclass(frozen=True)
class Disk:
    path: str
    size: int
    model: str
    serial: str
    identity: tuple
    mounts: tuple = ()
    blocked: str = ""

    def token(self):
        return hashlib.sha256(repr(self.identity).encode()).hexdigest()[:10]

    def confirmation(self):
        return f"ERASE {self.path} {self.token()}"


def device_identity(path):
    info = os.lstat(path)
    if not stat.S_ISBLK(info.st_mode):
        raise Refused("Target must be an actual whole block device, not a file, link, or partition")
    return info.st_rdev, info.st_ino, info.st_ctime_ns


def within(path, parent):
    return path == parent or path.startswith(parent.rstrip("/") + "/")


def linux_mounts(text):
    mounts, fuse_devices = {}, set()
    for line in text.splitlines():
        fields = line.split()
        if len(fields) < 10 or "-" not in fields:
            raise Refused("Cannot interpret the mount table safely")
        separator = fields.index("-")
        if separator < 6 or len(fields) < separator + 4 or not re.fullmatch(r"\d+:\d+", fields[2]):
            raise Refused("Cannot interpret the mount table safely")
        point = re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), fields[4])
        mounts.setdefault(fields[2], []).append(point)
        kind = fields[separator + 1]
        source = re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), fields[separator + 2])
        if kind == "fuseblk" or (kind.startswith("fuse.") and source.startswith("/dev/")):
            # ntfs-3g exposes an anonymous 0:N mount device, not the backing
            # partition's rdev. Neither st_dev nor mountinfo's third field can
            # protect that disk. Refuse it while mounted, including under /media.
            try:
                info = os.stat(source) if source.startswith("/dev/") else None
            except OSError as exc:
                raise Refused("Cannot identify a mounted FUSE block device; unmount it before flashing") from exc
            if info is None or not stat.S_ISBLK(info.st_mode):
                raise Refused("Cannot identify a mounted FUSE block device; unmount it before flashing")
            backing = f"{os.major(info.st_rdev)}:{os.minor(info.st_rdev)}"
            mounts.setdefault(backing, []).append(point)
            fuse_devices.add(backing)
    return mounts, fuse_devices


def flatten(node):
    yield node
    for child in node.get("children", []):
        yield from flatten(child)


def linux_child_mounts(node, mounts):
    reported = node.get("mountpoints") or []
    if not isinstance(reported, list) or any(point is not None and not isinstance(point, str) for point in reported):
        raise Refused("Cannot interpret block-device mountpoints safely")
    return set(mounts.get(node.get("maj:min"), [])) | {point for point in reported if point and point != "[SWAP]"}


def linux_reason(node, protected, mounts, holders, fuse_devices=()):
    if node.get("type") != "disk":
        return "not a whole disk"
    if node.get("log-sec") != 512:
        return "logical sector size is not 512 bytes"
    if node.get("ro") is not False and node.get("ro") != 0:
        return "read-only or unknown write state"
    if not (node.get("tran") == "usb" or node.get("rm") is True or node.get("rm") == 1):
        return "not removable or USB"
    for child in flatten(node):
        dev = child.get("maj:min")
        if dev in fuse_devices:
            return "contains an active FUSE block filesystem; unmount it before flashing"
        if not dev or dev in protected:
            return "contains system, source, staging, or current-directory storage"
        if child.get("type") not in ("disk", "part") or holders.get(dev, True):
            return "active mapped, RAID, encrypted, or otherwise held device"
        if child.get("fstype") in ("btrfs", "zfs_member", "LVM2_member", "linux_raid_member", "crypto_LUKS", "swap"):
            return "complex storage or swap; not eligible for automatic flashing"
        if "[SWAP]" in (child.get("mountpoints") or []):
            return "active swap"
        for point in linux_child_mounts(child, mounts):
            if not any(within(point, prefix) for prefix in ("/media", "/run/media")):
                return "mounted outside a removable-media directory"
    return ""


class LinuxDisks:
    def __init__(self, protected_paths):
        self.protected_paths = protected_paths

    def inventory(self):
        run("udevadm", "settle", "--timeout=10")
        tree = json.loads(run("lsblk", "--json", "--bytes", "--paths", "--tree", "--output",
                              "NAME,PATH,TYPE,SIZE,LOG-SEC,RO,RM,TRAN,MODEL,SERIAL,WWN,MAJ:MIN,MOUNTPOINTS,FSTYPE"))["blockdevices"]
        mounts, fuse_devices = linux_mounts(Path("/proc/self/mountinfo").read_text())
        protected = set()
        for path in self.protected_paths:
            dev = os.stat(path).st_dev
            protected.add(f"{os.major(dev)}:{os.minor(dev)}")
        for line in Path("/proc/swaps").read_text().splitlines()[1:]:
            name = re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), line.split()[0])
            info = os.stat(name)
            dev = info.st_rdev if stat.S_ISBLK(info.st_mode) else info.st_dev
            protected.add(f"{os.major(dev)}:{os.minor(dev)}")
        disks = []
        for node in tree:
            if node.get("type") != "disk":
                continue
            path = node.get("path")
            holders = {}
            for child in flatten(node):
                sysdev = Path("/sys/dev/block") / child["maj:min"]
                holders[child["maj:min"]] = bool(list((sysdev / "holders").iterdir()))
            reason = linux_reason(node, protected, mounts, holders, fuse_devices)
            ident = device_identity(path)
            if f"{os.major(ident[0])}:{os.minor(ident[0])}" != node["maj:min"]:
                raise Refused("Device identity changed during enumeration")
            sequence = Path("/sys/dev/block") / node["maj:min"] / "diskseq"
            if not sequence.exists():
                reason = reason or "kernel lacks disk-generation identity (Linux 5.15+ required)"
            identity = (path, *ident, sequence.read_text().strip() if sequence.exists() else "",
                        node.get("serial"), node.get("wwn"), node.get("model"), int(node["size"]))
            points = tuple(sorted({point for child in flatten(node) for point in linux_child_mounts(child, mounts)}))
            disks.append(Disk(path, int(node["size"]), node.get("model") or "Unknown model",
                              node.get("serial") or node.get("wwn") or "No serial", identity, points, reason))
        return disks

    def unmount(self, disk):
        for point in sorted(disk.mounts, key=len, reverse=True):
            run("umount", "--", point)  # Never lazy or forced: busy media aborts.

    def open(self, disk):
        fd = os.open(disk.path, os.O_RDWR | os.O_EXCL | os.O_NOFOLLOW)
        try:
            check_fd(fd, disk)
            check_geometry(fd, disk, "Linux")
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except BaseException:
            os.close(fd)
            raise

    def flush(self, fd):
        os.fsync(fd)
        # BLKFLSBUF invalidates Linux's block cache before the read-back pass.
        fcntl.ioctl(fd, 0x1261)

    def eject(self, disk):
        # No optional power-off command after closing: a renamed/replugged device
        # must never be acted on. The verified descriptor was already flushed.
        print("Drive flushed and verified. It is now safe to remove.")


def mac_reason(info, parts, protected, mounts):
    if info.get("WholeDisk") is not True or info.get("VirtualOrPhysical") != "Physical":
        return "not a whole physical disk"
    if info.get("Internal") is not False:
        return "internal disk or unknown location"
    if info.get("DeviceBlockSize") != 512:
        return "logical sector size is not 512 bytes"
    if info.get("Writable") is not True:
        return "read-only or unknown write state"
    if not (info.get("BusProtocol") == "USB" or info.get("RemovableMedia") is True):
        return "not removable or USB"
    if info.get("DeviceIdentifier") in protected:
        return "contains system, source, staging, or current-directory storage"
    if any(any(word in str(p.get("Content", "")) for word in ("APFS", "CoreStorage", "RAID")) for p in parts):
        return "APFS, encrypted, RAID, or multi-device storage is excluded"
    if any(not within(point, "/Volumes") for point in mounts):
        return "mounted outside /Volumes"
    if not info.get("DeviceTreePath"):
        return "missing hardware identity"
    return ""


class MacDisks:
    def __init__(self, protected_paths):
        self.protected_paths = protected_paths

    @staticmethod
    def info(path):
        return plistlib.loads(run("diskutil", "info", "-plist", str(path)))

    def inventory(self):
        listing = plistlib.loads(run("diskutil", "list", "-plist"))
        # st_dev identifies the containing filesystem even for arbitrary files,
        # APFS firmlinks, and directories. Never give file paths to diskutil info.
        protected_devices = {os.stat(path).st_dev for path in self.protected_paths}
        disks = []
        for entry in listing["AllDisksAndPartitions"]:
            path = "/dev/" + entry["DeviceIdentifier"]
            info = self.info(path)
            if info.get("WholeDisk") is not True:
                continue
            parts = [info] + entry.get("Partitions", [])
            mounts = tuple(sorted({str(p["MountPoint"]) for p in parts if p.get("MountPoint")}))
            # Query each partition as well: list output may omit mount metadata.
            for part in entry.get("Partitions", []):
                details = self.info("/dev/" + part["DeviceIdentifier"])
                parts.append(details)
                if details.get("MountPoint"):
                    mounts = tuple(sorted(set(mounts) | {details["MountPoint"]}))
            protected = set()
            for part in parts:
                device = "/dev/" + part["DeviceIdentifier"]
                if os.stat(device).st_rdev in protected_devices:
                    protected.add(info["DeviceIdentifier"])
            # APFS physical stores are excluded independently of their synthesized
            # volume IDs; CoreStorage/RAID are likewise never eligible.
            reason = mac_reason(info, parts, protected, mounts)
            ident = device_identity(path)
            size = int(info.get("TotalSize", 0))
            identity = (path, *ident, info.get("DeviceTreePath"), info.get("DiskUUID"),
                        info.get("MediaName"), size)
            disks.append(Disk(path, size, info.get("MediaName") or "Unknown model",
                              info.get("DiskUUID") or info.get("DeviceTreePath") or "Unknown", identity, mounts, reason))
        return disks

    def unmount(self, disk):
        run("diskutil", "unmountDisk", disk.path)  # No force argument.

    def open(self, disk):
        fd = os.open(disk.path, os.O_RDWR | os.O_EXLOCK | os.O_NONBLOCK | os.O_NOFOLLOW)
        try:
            check_fd(fd, disk)
            check_geometry(fd, disk, "Darwin")
            fcntl.fcntl(fd, 48, 1)  # Darwin F_NOCACHE: read back from media.
            return fd
        except BaseException:
            os.close(fd)
            raise

    def flush(self, fd):
        os.fsync(fd)
        fcntl.ioctl(fd, 0x20006416)  # Darwin DKIOCSYNCHRONIZECACHE.

    def eject(self, disk):
        # diskutil would reopen a path after releasing our descriptor; leave the
        # verified, unmounted disk alone so a hotplug cannot redirect an eject.
        print("Drive flushed and verified. It is now safe to remove.")


def ioctl_integer(fd, request, form):
    buffer = bytearray(struct.calcsize(form))
    fcntl.ioctl(fd, request, buffer, True)
    return struct.unpack(form, buffer)[0]


def check_geometry(fd, disk, system):
    if system == "Linux":
        size = ioctl_integer(fd, 0x80081272, "=Q")  # BLKGETSIZE64
        sector = ioctl_integer(fd, 0x1268, "=I")  # BLKSSZGET
        sequence = ioctl_integer(fd, 0x80081280, "=Q")  # BLKGETDISKSEQ
        if str(sequence) != disk.identity[4]:
            raise Refused("Disk generation changed before opening")
    else:
        sector = ioctl_integer(fd, 0x40046418, "=I")  # DKIOCGETBLOCKSIZE
        size = sector * ioctl_integer(fd, 0x40086419, "=Q")  # DKIOCGETBLOCKCOUNT
    if sector != 512 or size != disk.size:
        raise Refused("Opened disk size or logical sector size changed")


def check_fd(fd, disk):
    info = os.fstat(fd)
    if not stat.S_ISBLK(info.st_mode) or (info.st_rdev, info.st_ino, info.st_ctime_ns) != disk.identity[1:4]:
        raise Refused("Opened device no longer matches the selected disk")


def checked_disk(adapter, path, size, expected=None, unmounted=False):
    matches = [d for d in adapter.inventory() if d.path == path]
    if len(matches) != 1:
        raise Refused("Choose an exact whole-disk path from the list; links and partitions are refused")
    disk = matches[0]
    if disk.blocked:
        raise Refused(f"{path} is protected: {disk.blocked}")
    if disk.size < size:
        raise Refused("Selected disk is smaller than the uncompressed image")
    if expected is not None and disk.identity != expected.identity:
        raise Refused("Disk identity changed; unplug/replug requires starting again")
    if unmounted and disk.mounts:
        raise Refused("Disk is still mounted or was automatically remounted; close the file manager and retry")
    return disk


def copy_to_device(source, fd, size, expected_hash, progress=lambda phase, done, total: None):
    written, digest = 0, hashlib.sha256()
    while written < size:
        block = source.read(min(CHUNK, size - written))
        if not block:
            raise Refused("Staged image changed or became truncated during writing")
        digest.update(block)
        view = memoryview(block)
        while view:
            count = os.write(fd, view)
            if count <= 0:
                raise Refused("Short write to target")
            view = view[count:]
        written += len(block)
        progress("Writing", written, size)
    if source.read(1) or digest.hexdigest() != expected_hash:
        raise Refused("Staged image changed during writing; target is incomplete")


def read_back(fd, size, expected_hash, progress=lambda phase, done, total: None):
    os.lseek(fd, 0, os.SEEK_SET)
    checked, digest = 0, hashlib.sha256()
    while checked < size:
        block = os.read(fd, min(CHUNK, size - checked))
        if not block:
            raise Refused("Target read-back ended early")
        digest.update(block)
        checked += len(block)
        progress("Verifying", checked, size)
    if digest.hexdigest() != expected_hash:
        raise Refused("Read-back SHA-256 mismatch. Do not boot this drive; retry with another drive")


def clear_old_tail(fd, image_size, disk_size):
    # An old GPT backup at the physical end must not override the new image.
    # Never touch the image's own GPT or hybrid MBR; no automatic relocation.
    start = max(image_size, disk_size - MIB)
    if start < disk_size:
        os.lseek(fd, start, os.SEEK_SET)
        view = memoryview(b"\0" * (disk_size - start))
        while view:
            count = os.write(fd, view)
            if count <= 0:
                raise Refused("Short write while clearing stale partition metadata")
            view = view[count:]
    os.lseek(fd, 0, os.SEEK_SET)
    return start


def verify_old_tail(fd, start, disk_size):
    os.lseek(fd, start, os.SEEK_SET)
    remaining = disk_size - start
    while remaining:
        block = os.read(fd, remaining)
        if not block or any(block):
            raise Refused("Stale partition metadata clearing failed verification")
        remaining -= len(block)


def flash(adapter, disk, raw, size, digest, confirm, progress=lambda *args: None):
    if confirm != disk.confirmation():
        raise Refused("Confirmation did not match; nothing was erased")
    # Recheck before unmount, after unmount, and while the exact descriptor is held.
    disk = checked_disk(adapter, disk.path, size, disk)
    adapter.unmount(disk)
    disk = checked_disk(adapter, disk.path, size, disk, unmounted=True)
    fd = adapter.open(disk)
    try:
        checked_disk(adapter, disk.path, size, disk, unmounted=True)
        adapter.flush(fd)  # Unsupported flush/cache operations fail BEFORE erasure.
        with regular_input(raw) as source:
            if os.fstat(source.fileno()).st_size != size:
                raise Refused("Staged image size changed before writing")
            before = hashlib.sha256()
            while True:
                block = source.read(CHUNK)
                if not block:
                    break
                before.update(block)
            if before.hexdigest() != digest:
                raise Refused("Staged image SHA-256 changed before writing")
            source.seek(0)
            tail_start = clear_old_tail(fd, size, disk.size)
            copy_to_device(source, fd, size, digest, progress)
        adapter.flush(fd)
        read_back(fd, size, digest, progress)
        verify_old_tail(fd, tail_start, disk.size)
        adapter.flush(fd)
    finally:
        os.close(fd)
    adapter.eject(disk)


def show_disks(disks, size=0):
    for disk in disks:
        status = disk.blocked or ("too small for this image" if disk.size < size else "eligible")
        print(f"  {disk.path} | {disk.size / GIB:.2f} GiB | {printable(disk.model)} | {printable(disk.serial)} | {status}")


def parser():
    result = argparse.ArgumentParser(description="Download, verify, and flash Vaultboot to a chosen removable drive (Linux/macOS).")
    result.add_argument("--list", action="store_true", help="list disks and safety exclusions without writing")
    result.add_argument("--device", help="whole device from --list; still requires typed confirmation")
    result.add_argument("--target", choices=("amd64", "pi3-arm64"), help="PC USB or Raspberry Pi 3 microSD image")
    result.add_argument("--release", help="exact release tag; default newest published matching release, including previews")
    result.add_argument("--image", help="local official .img.gz; requires --manifest and --target")
    result.add_argument("--manifest", help="SHA256SUMS accompanying --image")
    result.add_argument("--cache-dir", default="/var/tmp", help="existing staging parent (default /var/tmp; needs compressed + raw image space)")
    result.add_argument("--no-preload", action="store_true", help="skip Obsidian preparation; destination needs internet for its first app setup")
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    if bool(args.image) != bool(args.manifest) or (args.image and not args.target):
        raise Refused("--image requires both --manifest and --target")
    system = platform.system()
    adapter_type = {"Linux": LinuxDisks, "Darwin": MacDisks}.get(system)
    if not adapter_type or (system == "Linux" and "microsoft" in platform.release().lower()):
        raise Refused("AUTOFLASH supports native Linux and macOS; use the documented manual flasher on Windows/WSL")
    protected = ["/", "/boot" if Path("/boot").exists() else "/", os.getcwd(), str(Path(__file__).resolve()), args.cache_dir]
    protected += [str(Path(p).resolve()) for p in (args.image, args.manifest) if p]
    adapter = adapter_type(protected)
    if args.list:
        show_disks(adapter.inventory())
        return 0
    if not sys.stdin.isatty():
        raise Refused("An interactive terminal is required; there is no unattended erase option")
    if os.geteuid() != 0:
        raise Refused("Run ./autoflash.sh to request administrator access, or sudo python3 scripts/autoflash.py")
    if not args.target:
        print("Image target: 1 = Intel/AMD PC USB; 2 = Raspberry Pi 3B/3B+ microSD")
        choice = input("Choose 1 or 2: ").strip()
        if choice not in ("1", "2"):
            raise Refused("No target selected")
        args.target = "amd64" if choice == "1" else "pi3-arm64"
    print("Vaultboot AUTOFLASH — the selected drive will be completely erased.")
    disks = adapter.inventory()
    if not any(not disk.blocked for disk in disks):
        show_disks(disks)
        raise Refused("No eligible removable drives. Insert a USB drive/card reader and retry")
    with tempfile.TemporaryDirectory(prefix="vaultboot-flash-", dir=args.cache_dir) as temporary:
        raw, size, digest = stage_image(args, Path(temporary))
        if not args.no_preload:
            digest = preload_image(args, raw, Path(temporary))
        disks = adapter.inventory()
        show_disks(disks, size)
        selected = args.device or input("Type the whole device path to erase (no default): ").strip()
        disk = checked_disk(adapter, selected, size)
        print(f"\nERASE ALL DATA ON {disk.path}: {printable(disk.model)}, {disk.size / GIB:.2f} GiB, identity {disk.token()}")
        print("Disconnect other removable drives if you are unsure. Close files on this drive.")
        print(f"Type exactly: {disk.confirmation()}")
        confirmation = input("> ").strip()
        last = [0.0, ""]
        def progress(phase, done, total):
            now = time.monotonic()
            if phase != last[1] or now - last[0] >= 2 or done == total:
                print(f"\r{phase}: {done / total:.0%} ({done / GIB:.2f}/{total / GIB:.2f} GiB)", end="\n" if done == total else "", flush=True)
                last[:] = [now, phase]
        flash(adapter, disk, raw, size, digest, confirmation, progress)
    print("Success. Every written image byte passed SHA-256 read-back verification.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nCancelled. If writing had started, the selected drive may contain an incomplete image.", file=sys.stderr)
        raise SystemExit(130)
    except (Refused, preload.PreloadError, OSError, ValueError, KeyError) as exc:
        print(f"AUTOFLASH stopped: {exc}", file=sys.stderr)
        print("If writing had started, the selected drive may be incomplete; do not boot it.", file=sys.stderr)
        raise SystemExit(1)
