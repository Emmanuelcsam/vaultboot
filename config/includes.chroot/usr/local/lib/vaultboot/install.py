#!/usr/bin/python3
"""Interactive, offline amd64 installer. Importing this module never probes disks.

The live squashfs is the only installation source. All disk writes are behind
whole-disk identity checks and a fresh, disk-specific terminal confirmation.
"""

import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import secrets
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import time


SOURCE = Path("/run/live/medium/live/filesystem.squashfs")
SOURCE_MANIFEST = Path("/run/live/medium/vaultboot-squashfs.sha256")
MIN_SIZE = 16 * 1024**3
ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8",
       "LC_ALL": "C.UTF-8", "DEBIAN_FRONTEND": "noninteractive"}
COMMANDS = ("lsblk", "udevadm", "parted", "wipefs", "mkfs.ext4", "mkfs.vfat",
            "unsquashfs", "blkid", "mount", "umount", "chroot", "grub-install",
            "update-grub", "update-initramfs", "unshare", "sync")
LIVE_PACKAGES = ("live-boot", "live-boot-initramfs-tools", "live-config",
                 "live-config-systemd", "live-tools", "user-setup")
VIRTUAL_FILESYSTEMS = {"proc", "sysfs", "tmpfs", "devtmpfs", "devpts", "ramfs",
                       "overlay", "cgroup", "cgroup2", "securityfs", "debugfs",
                       "tracefs", "pstore", "efivarfs", "configfs", "hugetlbfs",
                       "mqueue", "rpc_pipefs", "binfmt_misc", "autofs", "fusectl",
                       "nsfs", "bpf", "selinuxfs", "apparmorfs", "9p", "nfs", "nfs4",
                       "cifs", "smb3", "ceph", "afs", "fuse.sshfs"}


class InstallError(RuntimeError):
    pass


def run(*args, capture=False, input=None):
    result = subprocess.run(args, check=True, text=True, input=input, env=ENV,
                            stdout=subprocess.PIPE if capture else None)
    return result.stdout.strip() if capture else ""


def unescape(value):
    """Decode the kernel's octal escaping of mount/swap/backing-file names."""
    return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), value)


def dev_id(number):
    return f"{os.major(number)}:{os.minor(number)}"


def parse_mounts(text):
    rows = []
    for line in text.splitlines():
        fields = line.split()
        if len(fields) < 10 or "-" not in fields or not re.fullmatch(r"\d+:\d+", fields[2]):
            raise InstallError("Cannot safely interpret the mounted filesystems.")
        separator = fields.index("-")
        if separator + 3 >= len(fields):
            raise InstallError("Incomplete filesystem mount information.")
        rows.append((fields[2], unescape(fields[4]), fields[separator + 1]))
    return rows


def namespace_mounts(proc=Path("/proc")):
    """Include private service/container namespaces, not just our own mounts."""
    namespaces = set()
    rows = []
    roots = []
    for process in sorted(proc.iterdir()):
        if not process.name.isdecimal():
            continue
        try:
            namespace = (process / "ns/mnt").readlink()
            if namespace in namespaces:
                continue
            mounts = parse_mounts((process / "mountinfo").read_text())
        except (FileNotFoundError, ProcessLookupError):
            continue  # A process exited while being inspected.
        except PermissionError as error:
            raise InstallError("A mount namespace is inaccessible; no disk can be offered safely.") from error
        namespaces.add(namespace)
        roots.append(process / "root")
        rows.extend(mounts)
    if not rows:
        raise InstallError("Cannot inspect active mount namespaces.")
    return rows, roots


def block_inventory(sys_block=Path("/sys/class/block")):
    data = json.loads(run("lsblk", "--json", "--list", "--bytes", "--paths",
                          "--output", "NAME,KNAME,TYPE,SIZE,RO,MODEL,SERIAL,WWN,MAJ:MIN,PARTN",
                          capture=True))
    nodes = {}
    for entry in data.get("blockdevices", []):
        name = Path(entry["kname"]).name
        base = sys_block / name
        if not base.exists():
            raise InstallError("The disk list changed during inspection; try again.")
        identity = (base / "dev").read_text().strip()
        if identity != entry["maj:min"]:
            raise InstallError("Conflicting disk identities; no disks will be changed.")
        node = dict(entry)
        node["id"] = identity
        node["name"] = f"/dev/{name}"
        node["syspath"] = str(base.resolve(strict=True))
        node["size"] = int((base / "size").read_text()) * 512
        node["ro"] = bool(int((base / "ro").read_text()))
        node["diskseq"] = (base / "diskseq").read_text().strip() if (base / "diskseq").exists() else ""
        node["parents"] = set()
        if (base / "partition").exists():
            node["parents"].add((base.resolve().parent / "dev").read_text().strip())
            node["partn"] = int((base / "partition").read_text())
        if (base / "slaves").exists():
            for slave in (base / "slaves").iterdir():
                node["parents"].add((slave / "dev").read_text().strip())
        node["holders"] = {(holder / "dev").read_text().strip() for holder in (base / "holders").iterdir()}
        backing = base / "loop/backing_file"
        node["backing"] = unescape(backing.read_text().strip()) if backing.exists() else ""
        nodes[identity] = node
    if not nodes:
        raise InstallError("No block-device information is available.")
    for node in nodes.values():
        if (node["parents"] | node["holders"]) - nodes.keys():
            raise InstallError("An active storage dependency cannot be identified.")
    return nodes


def backing_device(path, roots):
    """Resolve a loop/swap file even if its path belongs to another namespace."""
    if not path.startswith("/") or path.endswith(" (deleted)"):
        raise InstallError("An active backing file cannot be identified safely.")
    devices = set()
    for candidate in [Path(path)] + [root / path.lstrip("/") for root in roots]:
        try:
            info = candidate.stat()
        except (FileNotFoundError, ProcessLookupError, NotADirectoryError):
            continue
        except PermissionError as error:
            raise InstallError("An active backing file is inaccessible.") from error
        if stat.S_ISBLK(info.st_mode):
            devices.add(dev_id(info.st_rdev))
        elif stat.S_ISREG(info.st_mode):
            devices.add(dev_id(info.st_dev))
    if not devices:
        raise InstallError(f"Cannot locate an active backing file: {path}")
    # A file on tmpfs/overlay has no block ID. All underlying mounted block
    # filesystems are protected separately, across every visible namespace.
    return devices


def mounted_devices(mounts, btrfs_root=Path("/sys/fs/btrfs")):
    """Anonymous Btrfs IDs hide their block devices; include every pool member.

    Unknown anonymous storage (for example ZFS or a block-backed FUSE mount)
    stops installation rather than silently treating a mounted disk as free.
    """
    devices = {row[0] for row in mounts if not row[0].startswith("0:")}
    for identity, _path, filesystem in mounts:
        if not identity.startswith("0:") or filesystem in VIRTUAL_FILESYSTEMS:
            continue
        if filesystem != "btrfs" or not btrfs_root.is_dir():
            raise InstallError(f"Cannot trace mounted {filesystem} storage safely; unmount it before installing.")
        members = set()
        for device in btrfs_root.glob("*/devices/*"):
            members.add((device / "dev").read_text().strip())
        if not members:
            raise InstallError("Mounted Btrfs storage has no identifiable block devices.")
        devices.update(members)
    return devices


def protected_devices(nodes, mounted, swaps, source_id):
    """Trace all active devices through partitions, dm/LVM/md and loop files."""
    protected = set()
    active = set(mounted) | set(swaps) | {source_id}
    for key, node in nodes.items():
        if node["holders"] or node["backing"]:
            active.add(key)

    def visit(key, trail):
        if key.startswith("0:"):
            return  # Virtual filesystem; mounted backing devices are separate.
        if key not in nodes:
            raise InstallError(f"Cannot trace active block device {key}; refusing disk writes.")
        if key in trail:
            raise InstallError("Cyclic storage dependencies; refusing disk writes.")
        if key in protected:
            return
        protected.add(key)
        for parent in nodes[key]["parents"]:
            visit(parent, trail | {key})

    for key in active:
        visit(key, set())
    return protected


def inspect_disks(source=SOURCE):
    run("udevadm", "settle", "--timeout=15")
    nodes = block_inventory()
    mounts, roots = namespace_mounts()
    for node in nodes.values():
        if node["backing"]:
            node["parents"].update(backing_device(node["backing"], roots))
    swaps = set()
    for line in Path("/proc/swaps").read_text().splitlines()[1:]:
        fields = line.split()
        if len(fields) < 5:
            raise InstallError("Cannot safely interpret active swap.")
        swaps.update(backing_device(unescape(fields[0]), roots))
    protected = protected_devices(nodes, mounted_devices(mounts), swaps, dev_id(source.stat().st_dev))
    return nodes, protected


def fingerprint(node):
    return tuple(node.get(key) for key in
                 ("id", "name", "syspath", "size", "diskseq", "serial", "wwn", "model", "type", "ro"))


def refusal(node, protected):
    if node["type"] != "disk" or node["parents"]:
        return "not a whole physical disk"
    if not node["diskseq"]:
        return "kernel disk identity is unavailable"
    if node["ro"]:
        return "read-only"
    if node["id"] in protected or node["holders"]:
        return "in use by mounted/live/system storage, swap or an active volume"
    if node["size"] < MIN_SIZE:
        return "smaller than 16 GiB"
    return ""


def check_device_node(node):
    path = Path(node["name"])
    info = path.lstat()
    if not stat.S_ISBLK(info.st_mode) or dev_id(info.st_rdev) != node["id"] or str(path.resolve()) != str(path):
        raise InstallError("The selected disk path is no longer the same whole block device.")


def revalidate(selected, source=SOURCE):
    nodes, protected = inspect_disks(source)
    current = nodes.get(selected["id"])
    if current is None or fingerprint(current) != fingerprint(selected):
        raise InstallError("The selected disk disappeared or changed. Nothing further will be written.")
    reason = refusal(current, protected)
    if reason:
        raise InstallError(f"The selected disk is now unsafe: {reason}.")
    check_device_node(current)
    return current


def check_identity(selected, descriptor=None):
    """Allow our own mounts, but never a changed, replaced or missing target."""
    current = block_inventory().get(selected["id"])
    if current is None or fingerprint(current) != fingerprint(selected):
        raise InstallError("The selected disk identity changed; refusing further writes.")
    check_device_node(current)
    if descriptor is not None:
        info = os.fstat(descriptor)
        if not stat.S_ISBLK(info.st_mode) or dev_id(info.st_rdev) != selected["id"]:
            raise InstallError("The held target descriptor is no longer the selected disk.")
        # BLKGETDISKSEQ identifies the open kernel disk, not a possibly reused
        # /dev path. Linux 6.12 in this image supplies this read-only ioctl.
        sequence = struct.unpack("=Q", fcntl.ioctl(descriptor, 0x80081280, bytes(8)))[0]
        if str(sequence) != selected["diskseq"]:
            raise InstallError("The held target disk was replaced; refusing further writes.")


class TargetGuard:
    """Hold an advisory disk lock while external partition/GRUB tools run."""

    def __init__(self, selected):
        self.selected = selected
        self.fd = None

    def __enter__(self):
        check_device_node(self.selected)
        self.fd = os.open(self.selected["name"], os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.check()
            # Probe kernel-exclusive access before the first write. Keeping
            # O_EXCL open would prevent the partitioning tools from working.
            probe = os.open(self.selected["name"], os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_EXCL)
            os.close(probe)
        except BaseException:
            os.close(self.fd)
            self.fd = None
            raise
        return self

    def check(self):
        check_identity(self.selected, self.fd)

    def __exit__(self, kind, value, traceback):
        os.close(self.fd)
        self.fd = None


def display(node):
    # Disk-supplied strings are untrusted terminal text, not escape sequences.
    clean = lambda value: "".join(char for char in str(value or "unknown") if char.isprintable())
    return (f"{node['name']} | {node['size'] / 1024**3:.1f} GiB | "
            f"{clean(node.get('model'))} | serial {clean(node.get('serial'))}")


def confirmation(node, nonce):
    return f"ERASE {node['name']} {node['size']} {nonce}"


def source_fingerprint(source=SOURCE):
    info = source.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_size < 1024**2:
        raise InstallError("The live installation source must be a regular squashfs file.")
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


def verify_source(source=SOURCE, manifest=SOURCE_MANIFEST):
    original = source_fingerprint(source)
    records = manifest.read_text().splitlines()
    if len(records) != 1:
        raise InstallError("The live source checksum manifest is missing or ambiguous.")
    match = re.fullmatch(r"([0-9a-fA-F]{64}) [ *](?:\./)?live/filesystem\.squashfs", records[0])
    if not match:
        raise InstallError("The live source checksum record is invalid.")
    digest = hashlib.sha256()
    print("Verifying the complete live installation source before any disk is erased…", flush=True)
    with source.open("rb") as stream:
        for block in iter(lambda: stream.read(4 * 1024**2), b""):
            digest.update(block)
    if source_fingerprint(source) != original or digest.hexdigest() != match[1].lower():
        raise InstallError("The live installation source failed its SHA256 check. Nothing was erased.")
    return original


def preflight():
    if os.geteuid() != 0:
        raise InstallError("Run sudo vaultboot-install from the live USB.")
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise InstallError("An interactive terminal is required. There is no unattended erase option.")
    if platform.machine() not in ("x86_64", "amd64"):
        raise InstallError("Internal-disk installation is supported only on amd64 PCs.")
    if "boot=live" not in Path("/proc/cmdline").read_text().split():
        raise InstallError("Boot the Vaultboot live USB to install onto another disk.")
    missing = [command for command in COMMANDS if not shutil.which(command, path=ENV["PATH"])]
    if missing:
        raise InstallError("This image lacks offline installer tools: " + ", ".join(missing))
    for target in ("i386-pc", "x86_64-efi"):
        if not Path(f"/usr/lib/grub/{target}/modinfo.sh").is_file():
            raise InstallError(f"This image lacks the {target} GRUB modules.")
    source_fingerprint()
    run("unsquashfs", "-stat", str(SOURCE), capture=True)
    # The wrapper unshares before Python starts. Never propagate target mounts.
    if os.readlink("/proc/self/ns/mnt") == os.readlink("/proc/1/ns/mnt"):
        raise InstallError("A private mount namespace is required; use vaultboot-install.")
    run("mount", "--make-rprivate", "/")


def choose_disk(nodes, protected):
    candidates = []
    print("\nVaultboot — fresh Debian installation\n")
    print("This erases EVERY partition and file on ONE selected disk.")
    print("Back up that disk first. Disconnect other backup/data disks if practical.")
    print("USB notes, Obsidian downloads, Wi-Fi settings and other live changes are NOT copied.")
    print("The operating system installs offline; the first Obsidian download needs internet.")
    print("The installed account is writer, password live, with autologin and local sudo.\n")
    for node in sorted(nodes.values(), key=lambda item: item["name"]):
        if node["type"] != "disk":
            continue
        reason = refusal(node, protected)
        if reason:
            print(f"Unavailable: {display(node)} ({reason})")
        else:
            candidates.append(node)
            print(f"  {len(candidates)}. {display(node)}")
    if not candidates:
        raise InstallError("No unused writable disk of at least 16 GiB is available. Nothing was changed.")
    answer = input("\nDisk number (Enter to cancel): ").strip()
    if not answer:
        raise InstallError("Cancelled. Nothing was changed.")
    if not answer.isdecimal() or not 1 <= int(answer) <= len(candidates):
        raise InstallError("Invalid disk selection. Nothing was changed.")
    selected = candidates[int(answer) - 1]
    phrase = confirmation(selected, secrets.token_hex(3).upper())
    print(f"\nERASE ALL DATA ON: {display(selected)}")
    print("This is a fresh install, with no dual boot, encryption or automatic note migration.")
    print("Type this exact line to erase that disk:")
    print(phrase)
    if input("> ") != phrase:
        raise InstallError("Confirmation did not match. Nothing was changed.")
    return selected


def partition_paths(selected):
    """Find actual kernel partition nodes, including nvme/mmc naming schemes."""
    for _ in range(20):
        run("udevadm", "settle", "--timeout=15")
        nodes = block_inventory()
        current = nodes.get(selected["id"])
        if current is None or fingerprint(current) != fingerprint(selected):
            raise InstallError("The target disk changed while partitioning.")
        partitions = {int(node["partn"]): node for node in nodes.values()
                      if node["type"] == "part" and node["parents"] == {selected["id"]}}
        if set(partitions) == {1, 2, 3}:
            for node in partitions.values():
                check_device_node(node)
            return {number: node["name"] for number, node in partitions.items()}
        time.sleep(0.25)
    raise InstallError("The three new target partitions did not appear; installation stopped.")


def uuid_for(device):
    value = run("blkid", "-s", "UUID", "-o", "value", device, capture=True)
    if not re.fullmatch(r"[0-9a-fA-F-]{4,64}", value):
        raise InstallError("Cannot determine a new filesystem UUID.")
    return value


def write(root, relative, text, mode=0o644):
    destination = root / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(text)
    destination.chmod(mode)


def configure_root(root, root_uuid, esp_uuid):
    """Configure only the extracted base; never read the live writer's home."""
    write(root, "etc/fstab", "# Vaultboot installed system\n"
          f"UUID={root_uuid} / ext4 defaults,noatime 0 1\n"
          f"UUID={esp_uuid} /boot/efi vfat umask=0077 0 2\n")
    write(root, "etc/default/grub", 'GRUB_DEFAULT=0\nGRUB_TIMEOUT=3\n'
          'GRUB_DISTRIBUTOR=Vaultboot\nGRUB_DISABLE_OS_PROBER=true\n'
          'GRUB_CMDLINE_LINUX_DEFAULT="quiet"\n'
          'GRUB_CMDLINE_LINUX="console=tty0 console=ttyS0,115200"\n')
    # An image may carry extra live-build GRUB defaults. Use only our installed
    # configuration so neither live nor persistence arguments can be re-added.
    snippets = root / "etc/default/grub.d"
    if snippets.exists():
        shutil.rmtree(snippets)
    write(root, "etc/hostname", "vaultboot\n")
    write(root, "etc/hosts", "127.0.0.1 localhost\n127.0.1.1 vaultboot\n::1 localhost ip6-localhost ip6-loopback\n")
    write(root, "etc/network/interfaces", "auto lo\niface lo inet loopback\n")
    resolver = root / "etc/resolv.conf"
    resolver.unlink(missing_ok=True)
    resolver.symlink_to("/run/NetworkManager/resolv.conf")
    write(root, "etc/locale.gen", "en_US.UTF-8 UTF-8\n")
    write(root, "etc/machine-id", "")
    machine_id = root / "var/lib/dbus/machine-id"
    machine_id.unlink(missing_ok=True)
    machine_id.parent.mkdir(parents=True, exist_ok=True)
    machine_id.symlink_to("/etc/machine-id")
    # Home is created later by useradd/copy from the image's clean /etc/skel.
    home = root / "home/writer"
    if home.is_symlink():
        home.unlink()
    elif home.exists():
        shutil.rmtree(home)


def chroot_setup(root, selected, guard):
    def inside(*args, **kwargs):
        return run("chroot", str(root), *args, **kwargs)

    # Prevent package triggers from launching daemons inside the target root.
    policy = root / "usr/sbin/policy-rc.d"
    saved = policy.read_bytes() if policy.exists() else None
    saved_mode = stat.S_IMODE(policy.stat().st_mode) if saved is not None else None
    write(root, "usr/sbin/policy-rc.d", "#!/bin/sh\nexit 101\n", 0o755)
    try:
        inside("apt-get", "-y", "--no-download", "purge", *LIVE_PACKAGES)
        # Reject a partial purge even if a future package manager changes its
        # exit behavior. No live configuration service remains in the install.
        installed = inside("dpkg-query", "-W", "-f=${binary:Package} ${db:Status-Abbrev}\\n", capture=True)
        if any(line.split()[0] in LIVE_PACKAGES and line.split()[1].startswith("ii")
               for line in installed.splitlines() if len(line.split()) >= 2):
            raise InstallError("Live boot packages remain in the installed root.")
        accounts = (root / "etc/passwd").read_text().splitlines()
        if not any(line.startswith("writer:") for line in accounts):
            inside("useradd", "--create-home", "--shell", "/bin/bash", "writer")
        else:
            inside("usermod", "--home", "/home/writer", "--shell", "/bin/bash", "writer")
            shutil.copytree(root / "etc/skel", root / "home/writer", symlinks=True)
            inside("chown", "-R", "writer:writer", "/home/writer")
        groups = {line.split(":")[0] for line in (root / "etc/group").read_text().splitlines()}
        memberships = sorted(groups & {"audio", "video", "render", "plugdev", "netdev", "sudo"})
        if memberships:
            inside("usermod", "--append", "--groups", ",".join(memberships), "writer")
        inside("passwd", "--lock", "root")
        inside("chpasswd", input="writer:live\n")
        write(root, "etc/sudoers.d/90-vaultboot", "writer ALL=(ALL:ALL) NOPASSWD: ALL\n", 0o440)
        inside("visudo", "-cf", "/etc/sudoers.d/90-vaultboot")
        inside("locale-gen")
        inside("update-locale", "LANG=en_US.UTF-8")
        inside("systemctl", "enable", "NetworkManager.service", "lightdm.service", "systemd-timesyncd.service")
        inside("systemctl", "set-default", "graphical.target")
        inside("update-initramfs", "-u", "-k", "all")
        guard.check()
        inside("grub-install", "--target=i386-pc", "--recheck", selected["name"])
        guard.check()
        inside("grub-install", "--target=x86_64-efi", "--efi-directory=/boot/efi",
               "--bootloader-id=Vaultboot", "--removable", "--no-nvram", "--no-uefi-secure-boot", "--recheck")
        inside("update-grub")
        grub = (root / "boot/grub/grub.cfg").read_text()
        kernel_lines = [line for line in grub.splitlines() if re.match(r"\s*linux\s", line)]
        if not kernel_lines or any("boot=live" in line or "persistence" in line for line in kernel_lines):
            raise InstallError("Installed GRUB configuration has no valid ordinary-root boot entry.")
        if not (root / "boot/efi/EFI/BOOT/BOOTX64.EFI").is_file():
            raise InstallError("The UEFI fallback loader was not created.")
    finally:
        if saved is None:
            policy.unlink(missing_ok=True)
        else:
            policy.write_bytes(saved)
            policy.chmod(saved_mode)


def install(selected, source=SOURCE, verified_source=None):
    source_id = source_fingerprint(source)
    if verified_source is None or source_id != verified_source:
        raise InstallError("A verified, unchanged live installation source is required before erasing.")
    mounts = []
    work = Path(tempfile.mkdtemp(prefix="vaultboot-install-", dir="/run"))
    root = work / "root"
    root.mkdir()
    completed = False
    guard = TargetGuard(selected)
    guard_open = False
    try:
        revalidate(selected, source)
        if source_fingerprint(source) != source_id:
            raise InstallError("The live installation source changed.")
        guard.__enter__()
        guard_open = True
        print("\nErasing the selected disk and creating BIOS, EFI and Linux partitions…", flush=True)
        # No destructive command occurs before the second live safety scan.
        guard.check()
        run("wipefs", "--all", "--", selected["name"])
        revalidate(selected, source)
        guard.check()
        run("parted", "--script", "--align", "optimal", selected["name"],
            "mklabel", "gpt", "mkpart", "BIOS", "1MiB", "3MiB", "set", "1", "bios_grub", "on",
            "mkpart", "EFI", "fat32", "3MiB", "515MiB", "set", "2", "esp", "on",
            "mkpart", "Vaultboot", "ext4", "515MiB", "100%")
        partitions = partition_paths(selected)
        # Automounters can react to newly created partitions. Refuse before
        # formatting if another namespace has claimed any part of the disk.
        revalidate(selected, source)
        guard.check()
        run("mkfs.vfat", "-F", "32", "-n", "VBOOT-EFI", partitions[2])
        revalidate(selected, source)
        guard.check()
        run("mkfs.ext4", "-F", "-L", "vaultboot-root", partitions[3])
        root_uuid, esp_uuid = uuid_for(partitions[3]), uuid_for(partitions[2])
        run("mount", "--", partitions[3], str(root))
        mounts.append(root)
        print("\nCopying the Debian base from the live USB (no network needed)…", flush=True)
        guard.check()
        run("unsquashfs", "-f", "-processors", "2", "-d", str(root), str(source))
        configure_root(root, root_uuid, esp_uuid)
        for directory in ("dev", "proc", "sys", "run", "boot/efi"):
            (root / directory).mkdir(parents=True, exist_ok=True)
        run("mount", "--", partitions[2], str(root / "boot/efi"))
        mounts.append(root / "boot/efi")
        for directory in ("dev", "proc", "sys"):
            target = root / directory
            run("mount", "--rbind", f"/{directory}", str(target))
            mounts.append(target)
            run("mount", "--make-rslave", str(target))
        run("mount", "-t", "tmpfs", "-o", "mode=755,nosuid,nodev", "tmpfs", str(root / "run"))
        mounts.append(root / "run")
        print("\nConfiguring the fresh user and installing both BIOS and UEFI boot files…", flush=True)
        guard.check()
        chroot_setup(root, selected, guard)
        run("sync")
        completed = True
    finally:
        failed = []
        for target in reversed(mounts):
            try:
                run("umount", "--recursive", str(target))
            except subprocess.CalledProcessError:
                failed.append(str(target))
        if guard_open:
            guard.__exit__(None, None, None)
        # Never recursively remove a possibly mounted target or shared tree.
        if not failed:
            root.rmdir()
            work.rmdir()
        else:
            raise InstallError("Could not unmount installation storage: " + ", ".join(failed) +
                               ". Shut down cleanly before removing any drive.")
    if completed:
        print("\nInstallation complete. Your USB notes have not been copied or changed.")
        print("Shut down, unplug the live USB, then select this disk in the firmware boot menu.")
        print("Keep Secure Boot disabled. Account: writer; password: live (change it with passwd).")
        print("Reconnect the network and download Obsidian on the installed system.")
        print("Restore your backed-up vault separately when ready. Do not unplug a running disk.")


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    if args:
        print("Usage: sudo vaultboot-install\nInteractive only; no automatic erase flags.", file=sys.stderr)
        return 2
    try:
        preflight()
        with open("/run/vaultboot-install.lock", "w") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise InstallError("Another installation is already running.") from error
            verified_source = verify_source()
            nodes, protected = inspect_disks()
            selected = choose_disk(nodes, protected)
            revalidate(selected)
            install(selected, verified_source=verified_source)
        return 0
    except (InstallError, OSError, subprocess.CalledProcessError, ValueError, KeyError) as error:
        print(f"\nInstallation stopped: {error}\nDo not boot a partially installed target.", file=sys.stderr)
        return 1
    except (EOFError, KeyboardInterrupt):
        print("\nInstallation interrupted. The target may be incomplete if erasing had begun.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
