"""Pinned offline package transport shared by AUTOFLASH and the appliance.

The raw slot is data, never a filesystem or executable. Its receipt cannot
authorize arbitrary software: every field must match the shipped manifest.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import struct
import tempfile
import uuid
import zlib

DEFAULT_MANIFEST = Path('/usr/share/vaultboot/obsidian-offline.json')
DEFAULT_CACHE = Path('/var/lib/vaultboot/offline')
TRUSTED_UID = 0
MAGIC = b'VAULTBOOT-OFF1\x00\x00'
HEADER_SIZE = 4096
SLOT_GUID = uuid.UUID('86f4a9d0-6c3e-4da6-9b18-3d0b37e4af21').bytes_le
CHUNK = 4 * 1024 * 1024
MAX_PACKAGE = 250 * 1024 * 1024
FIELDS = {'version', 'architecture', 'format', 'name', 'url', 'size', 'sha256'}


class PreloadError(RuntimeError):
    pass


def _json(data):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise PreloadError('Duplicate offline metadata key')
            result[key] = value
        return result
    try:
        return json.loads(data, object_pairs_hook=unique)
    except (ValueError, UnicodeError) as exc:
        raise PreloadError('Invalid offline metadata JSON') from exc


def load_manifest(path=DEFAULT_MANIFEST):
    with open(path, 'rb') as source:
        data = source.read(65537)
    if len(data) > 65536:
        raise PreloadError('Offline pin manifest is too large')
    result = _json(data)
    if not isinstance(result, dict) or result.get('schema') != 1 or not isinstance(result.get('assets'), dict):
        raise PreloadError('Unsupported offline pin manifest')
    return result


def asset_for(manifest, arch):
    value = manifest.get('assets', {}).get(arch)
    if not isinstance(value, dict) or set(value) != FIELDS:
        raise PreloadError('No pinned offline Obsidian package for this architecture')
    asset = dict(value)
    version, name = asset['version'], asset['name']
    if (not isinstance(version, str) or not re.fullmatch(r'v\d+\.\d+\.\d+', version)
            or not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9_.-]+', name)
            or asset['architecture'] != arch or arch not in ('amd64', 'arm64')
            or asset['format'] != ('deb' if arch == 'amd64' else 'tar')
            or type(asset['size']) is not int or not 0 < asset['size'] <= MAX_PACKAGE
            or not isinstance(asset['sha256'], str) or not re.fullmatch(r'[0-9a-f]{64}', asset['sha256'])
            or asset['url'] != f'https://github.com/obsidianmd/obsidian-releases/releases/download/{version}/{name}'):
        raise PreloadError('Invalid pinned offline package')
    return asset


def receipt_for(asset):
    return {'schema': 1, 'asset': dict(asset), 'terms_accepted': True}


def _receipt(value, asset):
    if (not isinstance(value, dict) or set(value) != {'schema', 'asset', 'terms_accepted'}
            or type(value['schema']) is not int or value['schema'] != 1
            or value['asset'] != asset or value['terms_accepted'] is not True):
        raise PreloadError('Offline receipt does not match the trusted pin or accepted terms')
    return value


def _read_at(stream, offset, count):
    stream.seek(offset)
    data = stream.read(count)
    if len(data) != count:
        raise PreloadError('Truncated offline image or package')
    return data


def _length(stream):
    stream.seek(0, os.SEEK_END)
    return stream.tell()


def _gpt(stream, lba, sectors):
    header = _read_at(stream, lba * 512, 512)
    if header[:8] != b'EFI PART':
        raise PreloadError('Invalid GPT signature')
    revision, length, crc = struct.unpack_from('<III', header, 8)
    current, backup, first, last = struct.unpack_from('<QQQQ', header, 24)
    table_lba, count, entry_size, table_crc = struct.unpack_from('<QIII', header, 72)
    if (revision != 0x10000 or not 92 <= length <= 512 or current != lba
            or not 0 < backup < sectors or backup == current
            or not 1 < first <= last < sectors or not 1 <= count <= 4096
            or entry_size < 128 or entry_size > 4096 or entry_size % 128
            or table_lba < 2 or table_lba * 512 + count * entry_size > sectors * 512):
        raise PreloadError('Invalid GPT bounds')
    table_end = table_lba + (count * entry_size + 511) // 512 - 1
    if (first <= current <= last or first <= backup <= last
            or (table_lba <= last and first <= table_end)
            or table_lba <= current <= table_end or table_lba <= backup <= table_end):
        raise PreloadError('GPT metadata overlaps usable partitions or headers')
    check = bytearray(header[:length])
    check[16:20] = b'\0' * 4
    if zlib.crc32(check) != crc:
        raise PreloadError('GPT header checksum mismatch')
    table = _read_at(stream, table_lba * 512, count * entry_size)
    if zlib.crc32(table) != table_crc:
        raise PreloadError('GPT partition table checksum mismatch')
    return header, table, entry_size, first, last, backup


def locate_slot(stream):
    """Return the dedicated partition's byte range, or None for old images.

    Validate both GPT copies. Legacy MBR-only images use the reserved 0xDA type.
    The caller must select the actual boot disk; this function never scans disks.
    """
    length = _length(stream)
    if length < 1024 or length % 512:
        raise PreloadError('Invalid disk length')
    mbr = _read_at(stream, 0, 512)
    if mbr[510:512] != b'\x55\xaa':
        raise PreloadError('Invalid disk signature')
    sectors = length // 512
    candidates, occupied = [], []
    if _read_at(stream, 512, 8) == b'EFI PART':
        head, table, step, first, last, backup = _gpt(stream, 1, sectors)
        other, other_table, other_step, other_first, other_last, other_backup = _gpt(stream, backup, sectors)
        if (other_backup != 1 or table != other_table or step != other_step
                or (first, last) != (other_first, other_last) or head[56:72] != other[56:72]):
            raise PreloadError('GPT copies disagree')
        for at in range(0, len(table), step):
            entry = table[at:at + step]
            if entry[:16] == bytes(16):
                continue
            start, end = struct.unpack_from('<QQ', entry, 32)
            if not first <= start <= end <= last:
                raise PreloadError('Partition exceeds GPT usable bounds')
            extent = (start * 512, (end - start + 1) * 512)
            (candidates if entry[:16] == SLOT_GUID else occupied).append(extent)
    else:
        for at in range(446, 510, 16):
            kind = mbr[at + 4]
            start, size = struct.unpack_from('<II', mbr, at + 8)
            if not kind:
                continue
            if kind == 0xEE:
                raise PreloadError('Protective MBR has no valid GPT')
            if start == 0 or size == 0 or start + size > sectors:
                raise PreloadError('Partition exceeds MBR bounds')
            (candidates if kind == 0xDA else occupied).append((start * 512, size * 512))
    if not candidates:
        return None
    if len(candidates) != 1:
        raise PreloadError('Multiple offline partitions')
    start, size = candidates[0]
    if size < HEADER_SIZE or size > 512 * 1024 * 1024:
        raise PreloadError('Invalid offline partition size')
    if any(start < offset + count and offset < start + size for offset, count in occupied):
        raise PreloadError('Offline partition overlaps another partition')
    return start, size


def _hash_stream(stream, size, output=None):
    digest = hashlib.sha256()
    remaining = size
    while remaining:
        data = stream.read(min(CHUNK, remaining))
        if not data:
            raise PreloadError('Truncated offline Obsidian package')
        remaining -= len(data)
        digest.update(data)
        if output is not None:
            output.write(data)
    return digest.hexdigest()


def read_slot(stream, offset, size, asset):
    if offset < 0 or size < HEADER_SIZE or offset + size > _length(stream):
        raise PreloadError('Offline partition exceeds disk bounds')
    header = _read_at(stream, offset, HEADER_SIZE)
    if header == bytes(HEADER_SIZE):
        return None
    if header[:16] != MAGIC:
        raise PreloadError('Offline partition header is corrupt')
    length = struct.unpack_from('<I', header, 16)[0]
    if not 0 < length <= HEADER_SIZE - 20 or any(header[20 + length:]):
        raise PreloadError('Invalid offline header length or padding')
    receipt = _receipt(_json(header[20:20 + length]), asset)
    if asset['size'] > size - HEADER_SIZE:
        raise PreloadError('Offline package exceeds its partition')
    if _hash_stream(stream, asset['size']) != asset['sha256']:
        raise PreloadError('Offline Obsidian package checksum mismatch')
    return receipt


def write_slot(stream, offset, size, asset, package):
    """Fill only an empty slot in a private staged image, never a target disk."""
    if read_slot(stream, offset, size, asset) is not None:
        raise PreloadError('Offline partition is already prepared')
    if asset['size'] > size - HEADER_SIZE:
        raise PreloadError('Pinned Obsidian package does not fit the offline partition')
    body = json.dumps(receipt_for(asset), sort_keys=True, separators=(',', ':')).encode()
    if len(body) > HEADER_SIZE - 20:
        raise PreloadError('Offline receipt is too large')
    package.seek(0)
    stream.seek(offset + HEADER_SIZE)
    if _hash_stream(package, asset['size'], stream) != asset['sha256'] or package.read(1):
        raise PreloadError('Downloaded Obsidian package does not match the trusted pin')
    # Publish the header last so partial preparations are never accepted.
    stream.seek(offset)
    stream.write(MAGIC + struct.pack('<I', len(body)) + body + bytes(HEADER_SIZE - 20 - len(body)))
    stream.flush()
    os.fsync(stream.fileno())
    read_slot(stream, offset, size, asset)


def _safe_dir(path, create=False):
    path = Path(path).absolute()
    for part in reversed((path, *path.parents)):
        try:
            info = part.lstat()
        except FileNotFoundError:
            if not create:
                raise PreloadError('Offline cache directory is missing')
            part.mkdir(mode=0o755)
            info = part.lstat()
        if not stat.S_ISDIR(info.st_mode):
            raise PreloadError('Offline cache path contains a link or non-directory')
        # A root-owned sticky temporary parent is safe for an exclusively
        # created root-owned child; ordinary writable ancestors are not.
        sticky_root = info.st_uid == 0 and info.st_mode & stat.S_ISVTX and part != path
        if info.st_uid not in (0, TRUSTED_UID) or (info.st_mode & 0o022 and not sticky_root):
            raise PreloadError('Offline cache has an unprotected ancestor')
    info = path.stat()
    if info.st_uid != TRUSTED_UID or info.st_mode & 0o022:
        raise PreloadError('Offline cache is not protected and root-owned')
    return path


def _safe_open(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != TRUSTED_UID or info.st_mode & 0o022:
            os.close(fd)
            raise PreloadError('Offline cache file is not protected and root-owned')
        return os.fdopen(fd, 'rb')
    except OSError as exc:
        raise PreloadError(f'Cannot safely open offline cache: {exc}') from exc


@dataclass
class CachedPackage:
    asset: dict
    stream: object
    receipt: dict


@contextmanager
def open_cache(arch, manifest_path=DEFAULT_MANIFEST, cache_dir=DEFAULT_CACHE):
    directory = Path(cache_dir)
    if not directory.exists() and not directory.is_symlink():
        yield None
        return
    _safe_dir(directory)
    if not any(directory.iterdir()):
        yield None
        return
    asset = asset_for(load_manifest(manifest_path), arch)
    with _safe_open(directory / 'ready.json') as source:
        data = source.read(HEADER_SIZE + 1)
        if len(data) > HEADER_SIZE:
            raise PreloadError('Offline receipt is too large')
        receipt = _receipt(_json(data), asset)
    with _safe_open(directory / asset['name']) as source:
        if os.fstat(source.fileno()).st_size != asset['size'] or _hash_stream(source, asset['size']) != asset['sha256']:
            raise PreloadError('Cached offline Obsidian package checksum mismatch')
        source.seek(0)
        yield CachedPackage(asset, source, receipt)


def copy_cached(cached, destination_dir):
    directory = _safe_dir(destination_dir, create=True)
    _receipt(cached.receipt, cached.asset)
    # Publish verified archive first and ready.json last. A crash is visible as
    # an incomplete cache, never success. No archive content is executed here.
    paths = []
    try:
        with tempfile.NamedTemporaryFile(dir=directory, prefix='.package-', delete=False) as output:
            paths.append(Path(output.name))
            cached.stream.seek(0)
            if _hash_stream(cached.stream, cached.asset['size'], output) != cached.asset['sha256'] or cached.stream.read(1):
                raise PreloadError('Offline package changed while copying')
            output.flush()
            os.fchmod(output.fileno(), 0o644)
            os.fsync(output.fileno())
        os.replace(paths[-1], directory / cached.asset['name'])
        with tempfile.NamedTemporaryFile(dir=directory, prefix='.receipt-', delete=False) as output:
            paths.append(Path(output.name))
            output.write(json.dumps(cached.receipt, sort_keys=True).encode() + b'\n')
            output.flush()
            os.fchmod(output.fileno(), 0o644)
            os.fsync(output.fileno())
        os.replace(paths[-1], directory / 'ready.json')
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        for path in paths:
            path.unlink(missing_ok=True)


def write_cache_from_slot(stream, offset, size, asset, cache_dir=DEFAULT_CACHE):
    receipt = read_slot(stream, offset, size, asset)
    if receipt is None:
        return None
    # Bound the stream to the package so copy_cached's trailing-byte check
    # cannot read unrelated bytes from the disk.
    with tempfile.TemporaryFile() as package:
        stream.seek(offset + HEADER_SIZE)
        if _hash_stream(stream, asset['size'], package) != asset['sha256']:
            raise PreloadError('Offline slot changed while reading')
        package.seek(0)
        copy_cached(CachedPackage(asset, package, receipt), cache_dir)
    return receipt
