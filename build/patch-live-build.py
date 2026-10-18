#!/usr/bin/env python3
"""Guarded corrections to trixie live-build's corresponding-source selection.

source_debian appends the bootloader identifier 'grub-efi', then requests it with
apt-get --only-source. Debian's source is called 'grub2'. Keep exact source-version
selection for installed packages and the strict missing-source release guard.
Also include Built-Using/Static-Built-Using, notably the unsigned Linux source
incorporated into signed kernel packages and statically linked Rust libraries.
"""

import argparse
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile

SUPPORTED = "1:20250505+deb13u1"
OLD = '\techo "${LB_BOOTLOADER_EFI}" >> source-selection.txt'
MARKER = "# Vaultboot: resolve the GRUB EFI binary identifier to its Debian source."
NEW = "\n".join((
    "\t" + MARKER,
    '\tcase "${LB_BOOTLOADER_EFI}" in',
    '\t\tgrub-efi) echo "grub2" >> source-selection.txt ;;',
    '\t\t*) echo "${LB_BOOTLOADER_EFI}" >> source-selection.txt ;;',
    "\tesac",
))
COLLECTOR_MARKER = "# Vaultboot: include exact sources for statically incorporated code."
COLLECTOR_OLD = """cat >> chroot/source-selection.sh << EOF
#!/bin/sh
dpkg-query -W -f='\\${source:Package}=\\${source:Version}\\n' | sort -u > source-selection.txt
EOF

Chroot chroot "sh source-selection.sh"
rm -f chroot/source-selection.sh
mv chroot/source-selection.txt ./"""


def collector_replacement():
    helper = Path(__file__).with_name("source-selection.py").read_text(encoding="utf-8")
    return (COLLECTOR_MARKER + "\npython3 - <<'VAULTBOOT_SOURCE_SELECTION' > source-selection.txt\n"
            + helper + "\nVAULTBOOT_SOURCE_SELECTION")


def patch(path, version):
    if version != SUPPORTED:
        raise ValueError(f"Review the live-build source workaround for version {version}; expected {SUPPORTED}")
    original = path.read_text(encoding="utf-8")
    updated = original
    if NEW not in updated and (updated.count(OLD) != 1 or MARKER in updated):
        raise ValueError("Unexpected source_debian content; refusing to patch")
    if NEW not in updated:
        updated = updated.replace(OLD, NEW, 1)
    collector = collector_replacement()
    if collector not in updated:
        if updated.count(COLLECTOR_OLD) != 1 or COLLECTOR_MARKER in updated:
            raise ValueError("Unexpected source-selection collector; refusing to patch")
        updated = updated.replace(COLLECTOR_OLD, collector, 1)
    if updated == original:
        return False
    subprocess.run(["sh", "-n"], input=updated, text=True, check=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".vaultboot-source-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), stat.S_IMODE(path.stat().st_mode))
            stream.write(updated)
            stream.flush()
            os.fsync(stream.fileno())
        # Keep an already-running shell's original file descriptor intact.
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", type=Path, default=Path("/usr/lib/live/build/source_debian"))
    parser.add_argument("--version")
    args = parser.parse_args()
    version = args.version or subprocess.check_output(
        ["dpkg-query", "-W", "-f=${Version}", "live-build"], text=True).strip()
    try:
        changed = patch(args.file, version)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"live-build source workaround failed: {exc}", file=sys.stderr)
        return 1
    print("Applied corresponding-source corrections" if changed else "Corresponding-source corrections already present")
    return 0


if __name__ == "__main__":
    sys.exit(main())
