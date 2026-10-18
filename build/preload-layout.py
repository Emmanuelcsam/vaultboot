#!/usr/bin/env python3
"""Reserve the empty offline slot in regular build images, never disk devices.

PC boot replay first appends MBR partition 4 as 0xDA. This helper assigns its
custom GPT type without asking xorriso to replace the hybrid boot record. The Pi
mode grows a finished image and relocates its backup GPT, preserving all existing
partition extents and the entire Pi firmware-visible hybrid MBR.
"""

import argparse
import contextlib
import fcntl
import json
import os
from pathlib import Path
import stat
import struct
import sys
import uuid
import zlib

SECTOR = 512
MIB = 1024 * 1024
SLOT_SIZE = 256 * MIB
SLOT_GUID = uuid.UUID("86f4a9d0-6c3e-4da6-9b18-3d0b37e4af21")
SLOT_NAME = "VAULTBOOT-OFFLINE"
EFI_GUID = uuid.UUID("c12a7328-f81f-11d2-ba4b-00a0c93ec93b")
ARM_ROOT_GUID = uuid.UUID("b921b045-1df0-41c3-af44-4c6f280d3fae")


@contextlib.contextmanager
def open_image(path, writable=False):
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode):
        raise ValueError("Image must be a regular file, not a symlink or device")
    if writable and (before.st_nlink != 1 or not before.st_mode & 0o222):
        raise ValueError("Writable image must have one link and writable permissions")
    fd = os.open(path, (os.O_RDWR if writable else os.O_RDONLY) | os.O_NOFOLLOW)
    try:
        after = os.fstat(fd)
        if (after.st_dev, after.st_ino, after.st_size, after.st_nlink) != (
                before.st_dev, before.st_ino, before.st_size, before.st_nlink):
            raise ValueError("Image identity changed while opening")
        fcntl.flock(fd, (fcntl.LOCK_EX if writable else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        with os.fdopen(fd, "r+b" if writable else "rb") as stream:
            fd = None
            yield stream
    finally:
        if fd is not None:
            os.close(fd)


def read_at(stream, offset, length):
    stream.seek(offset)
    data = stream.read(length)
    if len(data) != length:
        raise ValueError("Truncated GPT or partition extent")
    return data


def crc(data):
    return zlib.crc32(data) & 0xFFFFFFFF


def header(stream, lba, image_sectors):
    raw = read_at(stream, lba * SECTOR, SECTOR)
    if raw[:8] != b"EFI PART":
        raise ValueError("Missing GPT signature")
    revision, size, checksum, reserved = struct.unpack_from("<IIII", raw, 8)
    if revision != 0x10000 or size != 92 or reserved:
        raise ValueError("Unsupported GPT header format")
    checked = bytearray(raw[:size])
    struct.pack_into("<I", checked, 16, 0)
    if crc(checked) != checksum:
        raise ValueError("GPT header CRC mismatch")
    own, alternate, first, last = struct.unpack_from("<QQQQ", raw, 24)
    table_lba, count, entry_size, table_crc = struct.unpack_from("<QIII", raw, 72)
    if own != lba or not 0 < alternate < image_sectors or alternate == own:
        raise ValueError("Invalid GPT header location")
    if not 2 <= first <= last < image_sectors - 1:
        raise ValueError("Invalid GPT usable range")
    if entry_size != 128 or not 16 <= count <= 4096 or count * entry_size % SECTOR:
        raise ValueError("Unsupported GPT table dimensions")
    table_sectors = count * entry_size // SECTOR
    if not 2 <= table_lba or table_lba + table_sectors > image_sectors - 1:
        raise ValueError("GPT entry table is outside the image")
    if table_lba <= last and table_lba + table_sectors > first:
        raise ValueError("GPT entry table overlaps usable space")
    table = read_at(stream, table_lba * SECTOR, count * entry_size)
    if crc(table) != table_crc:
        raise ValueError("GPT entry table CRC mismatch")
    return {"raw": raw, "own": own, "alternate": alternate, "first": first, "last": last,
            "disk_guid": raw[56:72], "table_lba": table_lba, "count": count,
            "entry_size": entry_size, "table_sectors": table_sectors, "table": table}


def read_gpt(stream, allow_zero_tail=False):
    size = os.fstat(stream.fileno()).st_size
    if size % SECTOR or size < 2 * MIB:
        raise ValueError("Invalid image size for a 512-byte GPT")
    sectors = size // SECTOR
    mbr = read_at(stream, 0, SECTOR)
    if mbr[510:512] != b"\x55\xaa":
        raise ValueError("Missing hybrid MBR signature")
    primary = header(stream, 1, sectors)
    backup_lba = primary["alternate"]
    gpt_bytes = (backup_lba + 1) * SECTOR
    if gpt_bytes != size:
        # xorriso's output transport can add zero sectors after the backup GPT,
        # independently of the ISO padding setting. Remove only bounded zero
        # transport padding while finalizing the unpublished PC image.
        if not allow_zero_tail or not 0 < size - gpt_bytes <= MIB:
            raise ValueError("Backup GPT must be at image end")
        require_zero(stream, gpt_bytes, size - gpt_bytes)
    backup = header(stream, backup_lba, sectors)
    if primary["alternate"] != backup["own"] or backup["alternate"] != 1:
        raise ValueError("GPT primary/backup pointers disagree")
    for key in ("first", "last", "disk_guid", "count", "entry_size", "table"):
        if primary[key] != backup[key]:
            raise ValueError("GPT primary/backup contents disagree")
    if primary["table_lba"] + primary["table_sectors"] > primary["first"]:
        raise ValueError("Primary GPT entry table must precede usable space")
    if backup["table_lba"] <= backup["last"] or backup["table_lba"] + backup["table_sectors"] != backup["own"]:
        raise ValueError("Backup GPT entry table must immediately precede its header")
    entries = []
    for index in range(primary["count"]):
        raw = primary["table"][index * 128:(index + 1) * 128]
        if raw[:16] == bytes(16):
            continue
        first, last, flags = struct.unpack_from("<QQQ", raw, 32)
        if not primary["first"] <= first <= last <= primary["last"]:
            raise ValueError("GPT partition is outside the usable range")
        if raw[16:32] == bytes(16):
            raise ValueError("GPT partition has an empty unique GUID")
        entries.append({"number": index + 1, "type": uuid.UUID(bytes_le=raw[:16]),
                        "first": first, "last": last, "flags": flags,
                        "name": raw[56:128].decode("utf-16-le").rstrip("\0")})
    return {"size": size, "gpt_bytes": gpt_bytes, "mbr": mbr,
            "primary": primary, "backup": backup, "entries": entries}


def no_overlap(slot, entries):
    for part in entries:
        if part["number"] != slot["number"] and slot["first"] <= part["last"] and part["first"] <= slot["last"]:
            raise ValueError("Offline slot overlaps another partition")


def metadata(gpt):
    slots = [p for p in gpt["entries"] if p["type"] == SLOT_GUID]
    if len(slots) != 1:
        raise ValueError("Expected exactly one offline GPT partition")
    slot = slots[0]
    if slot["name"] != SLOT_NAME or (slot["last"] + 1 - slot["first"]) * SECTOR != SLOT_SIZE or slot["flags"]:
        raise ValueError("Unexpected offline partition name, size or attributes")
    no_overlap(slot, gpt["entries"])
    return {"offline_offset_bytes": slot["first"] * SECTOR, "offline_size_bytes": SLOT_SIZE,
            "offline_partition": slot["number"], "offline_type_guid": str(SLOT_GUID),
            "offline_name": SLOT_NAME}


def require_zero(stream, offset, size):
    zero = bytes(MIB)
    for at in range(offset, offset + size, MIB):
        length = min(MIB, offset + size - at)
        if read_at(stream, at, length) != zero[:length]:
            raise ValueError("Offline slot is not empty")


def updated_header(old, table, *, own=None, alternate=None, last=None, table_lba=None):
    raw = bytearray(old["raw"])
    for at, value in ((24, own), (32, alternate), (48, last), (72, table_lba)):
        if value is not None:
            struct.pack_into("<Q", raw, at, value)
    struct.pack_into("<I", raw, 88, crc(table))
    struct.pack_into("<I", raw, 16, 0)
    struct.pack_into("<I", raw, 16, crc(raw[:92]))
    return raw


def write_at(stream, offset, data):
    stream.seek(offset)
    if stream.write(data) != len(data):
        raise OSError("Short image write")


def write_tables(stream, primary, backup, table, *, new_size=None):
    if new_size is not None:
        stream.truncate(new_size)
    # Write the backup first. These unpublished regular build files are never
    # mounted while their partition tables are being finalized.
    for raw in (backup, primary):
        write_at(stream, struct.unpack_from("<Q", raw, 72)[0] * SECTOR, table)
        write_at(stream, struct.unpack_from("<Q", raw, 24)[0] * SECTOR, raw)
    stream.flush()
    os.fsync(stream.fileno())


def prepare_pc(path):
    with open_image(path, True) as stream:
        gpt = read_gpt(stream, allow_zero_tail=True)
        if any(p["type"] == SLOT_GUID for p in gpt["entries"]):
            raise ValueError("Image already contains an offline slot")
        raw = gpt["mbr"][494:510]
        start, sectors = struct.unpack_from("<II", raw, 8)
        if raw[4] != 0xDA or sectors * SECTOR != SLOT_SIZE:
            raise ValueError("Expected a 256 MiB MBR partition 4 of type 0xDA")
        matching = [p for p in gpt["entries"] if p["first"] == start and p["last"] + 1 == start + sectors]
        if len(matching) != 1:
            raise ValueError("GPT must expose the same offline extent as MBR partition 4")
        slot = matching[0]
        no_overlap(slot, gpt["entries"])
        for index in range(3):
            other = gpt["mbr"][446 + index * 16:462 + index * 16]
            first, count = struct.unpack_from("<II", other, 8)
            if count and start < first + count and first < start + sectors:
                raise ValueError("Offline slot overlaps an MBR partition")
        require_zero(stream, start * SECTOR, SLOT_SIZE)
        table = bytearray(gpt["primary"]["table"])
        at = (slot["number"] - 1) * 128
        table[at:at + 16] = SLOT_GUID.bytes_le
        table[at + 56:at + 128] = SLOT_NAME.encode("utf-16-le").ljust(72, b"\0")
        if slot["flags"]:
            raise ValueError("Unexpected attributes on appended offline partition")
        write_tables(stream, updated_header(gpt["primary"], table), updated_header(gpt["backup"], table),
                     table, new_size=gpt["gpt_bytes"])
        result = read_gpt(stream)
        if result["mbr"] != gpt["mbr"]:
            raise ValueError("Hybrid MBR changed unexpectedly")
        return metadata(result)


def append_pi(path):
    with open_image(path, True) as stream:
        gpt = read_gpt(stream)
        entries = {p["number"]: p for p in gpt["entries"]}
        if set(entries) != {1, 15} or entries[1]["type"] != ARM_ROOT_GUID or entries[15]["type"] != EFI_GUID:
            raise ValueError("Expected the official Pi GPT root 1 and firmware 15 only")
        root, firmware = entries[1], entries[15]
        if firmware["first"] < 2048 or firmware["last"] >= root["first"]:
            raise ValueError("Unexpected Pi firmware/root extents")
        raw = gpt["mbr"][446:462]
        start, sectors = struct.unpack_from("<II", raw, 8)
        if raw[4] != 0x0C or start != firmware["first"] or start + sectors != firmware["last"] + 1:
            raise ValueError("Pi hybrid MBR does not expose its firmware partition")
        table = bytearray(gpt["primary"]["table"])
        if table[128:256] != bytes(128):
            raise ValueError("Pi GPT partition 2 is not empty")
        slot_start = ((gpt["size"] + MIB - 1) // MIB) * MIB
        slot_end = slot_start + SLOT_SIZE
        tail_size = (gpt["backup"]["table_sectors"] + 1) * SECTOR
        new_size = ((slot_end + tail_size + MIB - 1) // MIB) * MIB
        backup_lba = new_size // SECTOR - 1
        backup_table_lba = backup_lba - gpt["backup"]["table_sectors"]
        table[128:144] = SLOT_GUID.bytes_le
        disk_guid = uuid.UUID(bytes_le=gpt["primary"]["disk_guid"])
        table[144:160] = uuid.uuid5(disk_guid, f"vaultboot-offline-{slot_start}").bytes_le
        struct.pack_into("<QQQ", table, 160, slot_start // SECTOR, slot_end // SECTOR - 1, 0)
        table[184:256] = SLOT_NAME.encode("utf-16-le").ljust(72, b"\0")
        primary = updated_header(gpt["primary"], table, alternate=backup_lba, last=backup_table_lba - 1)
        backup = updated_header(gpt["backup"], table, own=backup_lba, last=backup_table_lba - 1,
                                table_lba=backup_table_lba)
        write_tables(stream, primary, backup, table, new_size=new_size)
        # Old backup GPT is outside every original partition by read_gpt's bounds.
        write_at(stream, gpt["backup"]["table_lba"] * SECTOR, bytes(tail_size))
        stream.flush()
        os.fsync(stream.fileno())
        result = read_gpt(stream)
        if result["mbr"] != gpt["mbr"]:
            raise ValueError("Pi hybrid MBR changed unexpectedly")
        require_zero(stream, slot_start, SLOT_SIZE)
        return metadata(result)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("prepare-pc", "append-pi", "inspect"))
    parser.add_argument("image", type=Path)
    args = parser.parse_args()
    try:
        if args.mode == "inspect":
            with open_image(args.image) as stream:
                result = metadata(read_gpt(stream))
        else:
            result = (prepare_pc if args.mode == "prepare-pc" else append_pi)(args.image)
        print(json.dumps(result, indent=2))
    except (OSError, ValueError, UnicodeError) as exc:
        print(f"Cannot prepare offline partition: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
