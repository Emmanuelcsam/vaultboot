#!/usr/bin/env python3
"""Validate the supported official Debian Pi disk layout before image resizing."""

import argparse
import json
from pathlib import Path
import re
import stat
import struct
import subprocess


def validate_layout(table):
    if table.get("label") not in {"dos", "gpt"} or table.get("sectorsize") != 512:
        raise ValueError("Expected a DOS or GPT table with 512-byte sectors")
    partitions = table.get("partitions", [])
    if len(partitions) != 2:
        raise ValueError("Expected exactly two Debian Pi partitions")
    boot_types = {"c", "b", "0c", "0b", "ef", "ef00", "c12a7328-f81f-11d2-ba4b-00a0c93ec93b"}
    root_types = {"83", "8300", "0fc63daf-8483-4772-8e79-3d69d8477de4", "b921b045-1df0-41c3-af44-4c6f280d3fae"}
    boots = [p for p in partitions if str(p.get("type", "")).lower() in boot_types]
    roots = [p for p in partitions if str(p.get("type", "")).lower() in root_types]
    if len(boots) != 1 or len(roots) != 1:
        raise ValueError("Expected exactly one FAT firmware partition and one Linux root partition")
    boot, root = boots[0], roots[0]
    for partition in partitions:
        if not isinstance(partition.get("start"), int) or not isinstance(partition.get("size"), int):
            raise ValueError("Invalid partition extent")
        if partition["start"] < 2048 or partition["size"] <= 0:
            raise ValueError("Unsafe partition extent")
    if boot["start"] + boot["size"] > root["start"]:
        raise ValueError("Overlapping or unordered Pi partitions")
    return boot, root


def partition_number(table, partition):
    suffix = partition["node"].removeprefix(table["device"])
    if not re.fullmatch(r"p?[1-9][0-9]*", suffix):
        raise ValueError("Cannot identify the image partition number")
    return int(suffix.removeprefix("p"))


def validate_hybrid_mbr(header, boot):
    """Pi 3 firmware must see the FAT partition through the hybrid MBR."""
    if len(header) != 512 or header[510:512] != b"\x55\xaa":
        raise ValueError("Missing Pi hybrid MBR signature")
    entry = header[446:462]
    start, size = struct.unpack_from("<II", entry, 8)
    if entry[4] != 0x0C or start != boot["start"] or size != boot["size"]:
        raise ValueError("Pi hybrid MBR must expose the GPT firmware partition as its first FAT32 entry")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", type=Path)
    args = parser.parse_args()
    if args.image.is_symlink() or not stat.S_ISREG(args.image.stat().st_mode):
        parser.error("Input must be a regular image file, not a device or symlink")
    table = json.loads(subprocess.check_output(["sfdisk", "--json", str(args.image)], text=True))["partitiontable"]
    boot, root = validate_layout(table)
    if table["label"] == "gpt":
        with args.image.open("rb") as stream:
            validate_hybrid_mbr(stream.read(512), boot)
    if max(p["start"] + p["size"] for p in table["partitions"]) * 512 > args.image.stat().st_size:
        raise ValueError("Image does not contain its complete partition table extents")
    print(json.dumps({"partitiontable": table, "boot_partition": partition_number(table, boot),
                      "root_partition": partition_number(table, root)}, indent=2))


if __name__ == "__main__":
    main()
