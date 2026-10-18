#!/usr/bin/python3
"""Copy a prepared package from this boot disk before the writer session starts.

Importing this module performs no device access. Only the mounted live medium,
or the ordinary root filesystem, identifies a candidate; other disks are never
searched. Obsidian extraction and execution remain in the normal-user runtime.
"""

from contextlib import contextmanager
import os
from pathlib import Path
import platform
import re
import signal
import stat
import sys
import tempfile

import preload


STATE = Path("/var/lib/vaultboot")
ERROR = STATE / "offline-error.txt"


def architecture():
    machine = platform.machine().lower()
    if machine in {"x86_64", "amd64"}:
        return "amd64"
    if machine in {"aarch64", "arm64"}:
        return "arm64"
    raise preload.PreloadError("This computer architecture has no prepared Obsidian package.")


def mounted_boot_id(text):
    selected = {}
    for line in text.splitlines():
        fields = line.split()
        if len(fields) < 10 or "-" not in fields:
            raise preload.PreloadError("Cannot identify the boot filesystem safely.")
        mountpoint = re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), fields[4])
        if mountpoint not in {"/", "/run/live/medium"}:
            continue
        if (mountpoint in selected and selected[mountpoint] != fields[2]) or not re.fullmatch(r"[0-9]+:[0-9]+", fields[2]):
            raise preload.PreloadError("The boot filesystem has an ambiguous device identity.")
        selected[mountpoint] = fields[2]
    identity = selected.get("/run/live/medium", selected.get("/"))
    # A network, RAM, or unsupported overlay root is not a license to search
    # attached drives. The ordinary online setup remains available.
    return identity if identity and not identity.startswith("0:") else None


def boot_device(mountinfo=Path("/proc/self/mountinfo"), sys_devices=Path("/sys/dev/block")):
    identity = mounted_boot_id(mountinfo.read_text())
    if identity is None:
        return None
    device = (sys_devices / identity).resolve(strict=True)
    if (device / "dev").read_text().strip() != identity:
        raise preload.PreloadError("The boot device changed during inspection.")
    if (device / "partition").exists():
        device = device.parent
    optical_type = device / "device/type"
    if optical_type.is_file() and optical_type.read_text().strip() == "5":
        return None  # CD/DVD ISO sessions have no prepared raw slot.
    if any((device / "slaves").iterdir()) or (device / "loop").exists():
        # Device-mapper, RAID and loop-backed roots need a different discovery
        # contract. Do not guess which physical member contains a payload.
        return None
    values = dict(line.split("=", 1) for line in (device / "uevent").read_text().splitlines() if "=" in line)
    name = values.get("DEVNAME", "")
    identity = (device / "dev").read_text().strip()
    if values.get("DEVTYPE") != "disk" or not re.fullmatch(r"[A-Za-z0-9_.!-]+", name) or not re.fullmatch(r"[0-9]+:[0-9]+", identity):
        raise preload.PreloadError("Cannot resolve the boot filesystem to one whole disk.")
    return Path("/dev") / name, identity


@contextmanager
def open_boot_device(device, identity):
    descriptor = os.open(device, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        actual = f"{os.major(info.st_rdev)}:{os.minor(info.st_rdev)}"
        if not stat.S_ISBLK(info.st_mode) or actual != identity:
            raise preload.PreloadError("The boot disk identity changed before it could be read.")
        with os.fdopen(descriptor, "rb", buffering=0) as stream:
            descriptor = None
            yield stream
    finally:
        if descriptor is not None:
            os.close(descriptor)


def report_error(message):
    STATE.mkdir(parents=True, exist_ok=True, mode=0o755)
    info = STATE.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        raise preload.PreloadError("The offline package status directory is not owned safely by root.")
    descriptor, temporary = tempfile.mkstemp(prefix=".offline-error-", dir=STATE)
    try:
        with os.fdopen(descriptor, "w") as output:
            output.write("The prepared Obsidian package could not be loaded.\n" + str(message)[:2000] + "\n")
            output.flush()
            os.fsync(output.fileno())
            os.fchmod(output.fileno(), 0o644)
        os.replace(temporary, ERROR)
    finally:
        Path(temporary).unlink(missing_ok=True)


def prepare():
    if os.geteuid() != 0:
        raise preload.PreloadError("The boot package-copy service must run as root.")
    arch = architecture()
    cache_error = None
    try:
        with preload.open_cache(arch) as cached:
            if cached is not None:
                ERROR.unlink(missing_ok=True)
                return "cached"
    except preload.PreloadError as exc:
        cache_error = exc
    device = boot_device()
    if device is not None:
        with open_boot_device(*device) as stream:
            slot = preload.locate_slot(stream)
            if slot is not None:
                asset = preload.asset_for(preload.load_manifest(), arch)
                receipt = preload.write_cache_from_slot(stream, *slot, asset)
                if receipt is not None:
                    ERROR.unlink(missing_ok=True)
                    return "prepared"
    if cache_error is not None:
        raise cache_error
    ERROR.unlink(missing_ok=True)
    return "absent"


def main():
    def timeout(_signal, _frame):
        raise preload.PreloadError("Copying the prepared package took too long. Check the boot drive and prepare it again.")

    signal.signal(signal.SIGTERM, timeout)
    try:
        print("Vaultboot offline package:", prepare(), flush=True)
        return 0
    except (preload.PreloadError, OSError, ValueError) as exc:
        print("Vaultboot offline package:", exc, file=sys.stderr, flush=True)
        try:
            report_error(exc)
        except (preload.PreloadError, OSError) as status_error:
            print("Could not record offline package status:", status_error, file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
