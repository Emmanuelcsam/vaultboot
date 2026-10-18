#!/usr/bin/env python3
"""Add persistence to a new hybrid image; never open a disk device for writing.

xorriso's boot replay regenerates the hybrid MBR/GPT and preserves both El Torito
boot entries. A raw ext4 image is appended outside the ISO, not inside its readonly
filesystem. No loop devices, mounting, or root permission are required.
"""

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import stat
import struct
import subprocess
import sys
import tempfile

SECTOR = 512
MIB = 1024 * 1024
LABEL = b"vaultboot-data"


def run(*args):
    result = subprocess.run(args, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if result.returncode:
        raise RuntimeError(f"Command failed ({result.returncode}): {' '.join(map(str, args))}\n{result.stdout}")
    return result.stdout


def boot_report(path):
    report = run("xorriso", "-indev", str(path), "-report_el_torito", "plain", "-report_system_area", "plain")
    for platform in ("BIOS", "UEFI"):
        if not re.search(r"El Torito boot img\s*:.*\b" + platform + r"\b", report):
            raise ValueError(f"Image is missing the {platform} boot entry")
    return report


def partitions(path):
    """Read the hybrid MBR, allowing its pre-existing ISO/EFI nesting only."""
    with path.open("rb") as stream:
        mbr = stream.read(SECTOR)
    if len(mbr) != SECTOR or mbr[510:512] != b"\x55\xaa":
        raise ValueError("Image does not have a hybrid MBR")
    entries = []
    for index in range(4):
        raw = mbr[446 + index * 16:462 + index * 16]
        start, sectors = struct.unpack_from("<II", raw, 8)
        if sectors:
            entries.append({"number": index + 1, "type": raw[4], "start": start, "sectors": sectors})
    return entries


def check_output(path, expected_bytes):
    entries = partitions(path)
    added = next((p for p in entries if p["number"] == 3 and p["type"] == 0x83), None)
    if not added or added["sectors"] * SECTOR != expected_bytes:
        raise ValueError("Missing or incorrectly sized Linux persistence partition")
    start = added["start"]
    end = start + added["sectors"]
    for part in entries:
        if part["number"] != 3 and start < part["start"] + part["sectors"] and part["start"] < end:
            raise ValueError(f"Persistence overlaps existing MBR partition {part['number']}")
    if end * SECTOR > path.stat().st_size:
        raise ValueError("Persistence partition extends past the image")
    with path.open("rb") as stream:
        stream.seek(start * SECTOR + 1024)
        superblock = stream.read(1024)
        if superblock[56:58] != b"\x53\xef" or superblock[120:136].rstrip(b"\x00") != LABEL:
            raise ValueError("Appended partition is not the expected labeled ext4 filesystem")
        # Some hybrid ISOs also expose GPT. Ensure the same appended extent is
        # visible there and never overlaps any pre-existing GPT extent.
        stream.seek(SECTOR)
        gpt = stream.read(SECTOR)
        if gpt[:8] == b"EFI PART":
            table_lba, count, size = struct.unpack_from("<QII", gpt, 72)
            if count > 4096 or size < 128 or size > 4096:
                raise ValueError("Unexpected GPT table dimensions")
            stream.seek(table_lba * SECTOR)
            table = stream.read(count * size)
            matching = False
            for offset in range(0, len(table), size):
                entry = table[offset:offset + size]
                if entry[:16] == b"\x00" * 16:
                    continue
                first, last = struct.unpack_from("<QQ", entry, 32)
                if first == start and last + 1 == end:
                    matching = True
                elif start <= last and first < end:
                    raise ValueError("Persistence overlaps another GPT partition")
            if not matching:
                raise ValueError("GPT does not expose the persistence partition")
    return entries


def make_image(iso, output, persistence_mib):
    if not stat.S_ISREG(iso.lstat().st_mode):
        raise ValueError("Input must be a regular ISO file, not a symlink or device")
    if not output.parent.is_dir():
        raise ValueError("Output parent directory must already exist")
    report_path = output.with_name(output.name + ".layout.json")
    for path in (output, report_path):
        if os.path.lexists(path):
            raise ValueError(f"Refusing existing path: {path}")
    for program in ("xorriso", "mkfs.ext4", "debugfs", "e2fsck"):
        if not shutil.which(program):
            raise ValueError(f"Required program not found: {program}")
    boot_report(iso)
    if any(p["number"] >= 3 for p in partitions(iso)):
        raise ValueError("Expected a fresh Vaultboot ISO with free MBR partitions 3 and 4")
    expected_bytes = persistence_mib * MIB
    with tempfile.TemporaryDirectory(prefix=".vaultboot-image-", dir=output.parent) as temp:
        work = Path(temp)
        root = work / "root"
        root.mkdir()
        (root / "persistence.conf").write_text("/ union\n", encoding="ascii")
        filesystem = work / "persistence.ext4"
        with filesystem.open("xb") as stream:
            stream.truncate(expected_bytes)
        run("mkfs.ext4", "-F", "-q", "-L", LABEL.decode(), "-m", "0", "-d", str(root), str(filesystem))
        if "/ union" not in run("debugfs", "-R", "cat persistence.conf", str(filesystem)):
            raise ValueError("Persistence configuration was not written")
        run("e2fsck", "-f", "-n", str(filesystem))
        candidate = work / "usb.img"
        run("xorriso", "-indev", str(iso), "-outdev", str(candidate),
            "-boot_image", "any", "replay", "-append_partition", "3", "0x83", str(filesystem),
            "-commit", "-end")
        report = boot_report(candidate)
        entries = check_output(candidate, expected_bytes)
        layout = {"persistence_label": LABEL.decode(), "persistence_bytes": expected_bytes,
                  "image_bytes": candidate.stat().st_size, "mbr_partitions": entries,
                  "boot_report": report}
        # Hard-link publication is atomic and fails if another process created
        # the requested path in the meantime. Existing files are never replaced.
        os.link(candidate, output)
        with report_path.open("x", encoding="utf-8") as stream:
            json.dump(layout, stream, indent=2)
            stream.write("\n")
    print(f"Created {output} ({output.stat().st_size} bytes), persistence {persistence_mib} MiB")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iso", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--persistence-mib", type=int, default=4096)
    args = parser.parse_args()
    if not 64 <= args.persistence_mib <= 1024 * 128:
        parser.error("Persistence must be 64 MiB through 128 GiB")
    try:
        make_image(args.iso.absolute(), args.output.absolute(), args.persistence_mib)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"Cannot create USB image: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
