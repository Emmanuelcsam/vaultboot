#!/usr/bin/env python3
"""Boot an ARM64 candidate on QEMU virt using its own kernel and initramfs.

This checks ARM software under emulation, not Raspberry Pi firmware, graphics,
Wi-Fi, or physical hardware support. Extract the kernel/initramfs from the exact
candidate being tested. The raw image remains read-only behind a qcow2 overlay.
No TCP monitor, VNC, forwarded ports, host devices, or shared folders are used.
"""
import argparse
import json
from pathlib import Path
import subprocess

from console import VM, run_console


def qemu_arguments(overlay, kernel, initrd, serial_path, qmp_path, offline=False, memory=1024):
    if memory not in (1024, 2048):
        raise ValueError("ARM guest memory must be 1024 or 2048 MiB")
    args = [
        "qemu-system-aarch64", "-machine", "virt", "-accel", "tcg,thread=multi",
        "-cpu", "cortex-a53", "-smp", "2", "-m", str(memory),
        "-kernel", str(kernel), "-initrd", str(initrd),
        "-append", "root=/dev/vda1 rw rootwait console=tty0 console=ttyAMA0,115200",
        "-drive", f"file={overlay},format=qcow2,if=virtio",
        "-device", "virtio-gpu-pci", "-device", "qemu-xhci",
        "-device", "usb-kbd", "-device", "usb-tablet", "-display", "none",
        "-serial", f"unix:{serial_path},server=on,wait=off",
        "-qmp", f"unix:{qmp_path},server=on,wait=off",
    ]
    args += (["-nic", "none"] if offline else [
        "-netdev", "user,id=net0", "-device", "virtio-net-pci,netdev=net0,id=nic0",
    ])
    return args


class ARMVM(VM):
    def __init__(self, image, kernel, initrd, work, offline=False, memory=1024):
        inputs = [Path(value).resolve(strict=True) for value in (image, kernel, initrd)]
        if not all(path.is_file() for path in inputs):
            raise ValueError("Image, kernel and initramfs must be regular files")
        image, kernel, initrd = inputs
        self.work = Path(work).resolve()
        self.work.mkdir(parents=True, exist_ok=True)
        self.serial_path = self.work / "serial.sock"
        self.qmp_path = self.work / "qmp.sock"
        overlay = self.work / "disk.qcow2"
        if overlay.is_symlink():
            raise ValueError("Test overlay must not be a symlink")
        if overlay.exists():
            metadata = json.loads(subprocess.check_output([
                "qemu-img", "info", "--output=json", str(overlay),
            ], text=True))
            backing = metadata.get("full-backing-filename") or metadata.get("backing-filename", "")
            if metadata.get("format") != "qcow2" or Path(backing).resolve() != image:
                raise ValueError("Existing overlay belongs to a different image; use a new work directory")
        else:
            subprocess.run([
                "qemu-img", "create", "-f", "qcow2", "-F", "raw", "-b", str(image), str(overlay),
            ], check=True)
        self.start_process(qemu_arguments(overlay, kernel, initrd, self.serial_path, self.qmp_path, offline, memory))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", type=Path, help="uncompressed candidate .img")
    parser.add_argument("kernel", type=Path, help="vmlinuz extracted from that candidate")
    parser.add_argument("initrd", type=Path, help="initrd.img extracted from that candidate")
    parser.add_argument("work", type=Path, help="private output/overlay/command queue directory")
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--memory", type=int, default=1024, choices=(1024, 2048),
                        help="guest MiB; 1024 matches Pi 3B/B+ capacity, 2048 is a diagnostic comparison")
    parser.add_argument("--login-timeout", type=int, default=120, choices=range(30, 1801))
    parser.add_argument("--duration", type=int, default=3600, choices=range(60, 10801))
    args = parser.parse_args()
    vm = ARMVM(args.image, args.kernel, args.initrd, args.work, args.offline, args.memory)
    run_console(vm, args.work, args.login_timeout, args.duration)


if __name__ == "__main__":
    main()
