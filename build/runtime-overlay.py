#!/usr/bin/env python3
"""Apply a narrow, auditable Pi runtime correction to a private image copy.

The release builder is the caller. This helper neither opens block devices nor
accepts arbitrary destination paths; only the two declared module files can
change, and every old file must match the original build's git object first.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

ALLOWED = (
    "config/includes.chroot/usr/local/lib/vaultboot/runtime.py",
    "config/includes.chroot/usr/local/lib/vaultboot/install.py",
)
BUILD_INPUTS = ("build/pi-layout.py", "build/pi-sources.py", "build/source-selection.py")
PACKAGES = "config/package-lists/vaultboot.list.chroot"


def git(project, *args):
    return subprocess.check_output(["git", "-C", str(project), *args], stderr=subprocess.PIPE)


def plan(project, base, final):
    if not all(re.fullmatch(r"[a-f0-9]{40}", value) for value in (base, final)):
        raise ValueError("Exact git commit IDs are required")
    if git(project, "rev-parse", "HEAD").decode().strip() != final:
        raise ValueError("Checkout does not match the final source commit")
    if git(project, "status", "--porcelain", "--untracked-files=no"):
        raise ValueError("Tracked checkout files are dirty")
    if git(project, "diff", "--name-only", base, final, "--", *BUILD_INPUTS):
        raise ValueError("Original Pi build inputs changed; a full build is required")
    old_build = git(project, "show", f"{base}:build/pi.sh")
    new_build = git(project, "show", f"{final}:build/pi.sh")
    expected = old_build.replace(b"grub2-common|firmware-", b"grub2-common|fdisk|firmware-", 1)
    if new_build not in (old_build, expected):
        raise ValueError("Original Pi build inputs changed; a full build is required")
    old_packages = git(project, "show", f"{base}:{PACKAGES}").decode().splitlines()
    new_packages = git(project, "show", f"{final}:{PACKAGES}").decode().splitlines()
    if new_packages != old_packages:
        if (new_packages.count("fdisk") != 1 or "fdisk" in old_packages
                or [line for line in new_packages if line != "fdisk"] != old_packages):
            raise ValueError("Package changes other than the excluded PC fdisk dependency require a full build")
    def effective(lines, script):
        patterns = re.findall(rb"grep -Ev '([^']+)'", script)
        if len(patterns) != 1:
            raise ValueError("Cannot prove effective Pi package selection")
        excluded = re.compile(patterns[0].decode())
        return [line for line in lines if line.strip() and not line.lstrip().startswith("#") and not excluded.search(line)]
    if effective(old_packages, old_build) != effective(new_packages, new_build):
        raise ValueError("Effective Pi package selection changed; a full build is required")
    changed = git(project, "diff", "--name-only", "--no-renames", base, final, "--", "config").decode().splitlines()
    changed = [name for name in changed if name != PACKAGES]
    if not changed or any(name not in ALLOWED for name in changed):
        raise ValueError("Only runtime.py and install.py corrections are supported; packages/hooks/other config require a full build")
    entries = []
    for name in changed:
        old = git(project, "show", f"{base}:{name}")
        new = git(project, "show", f"{final}:{name}")
        for commit in (base, final):
            if not git(project, "ls-tree", commit, "--", name).startswith(b"100644 blob "):
                raise ValueError("Runtime modules must remain ordinary mode-0644 files")
        entries.append({"path": name, "old_sha256": hashlib.sha256(old).hexdigest(),
                        "new_sha256": hashlib.sha256(new).hexdigest(), "content": new})
    return entries


def apply(project, root, base, final):
    entries = plan(project, base, final)
    destinations = []
    root = Path(root).resolve(strict=True)
    # Validate every original before changing any destination, including the
    # second file. A corrupt/unexpected image never receives a partial overlay.
    for entry in entries:
        relative = Path(entry["path"]).relative_to("config/includes.chroot")
        destination = root / relative
        for candidate in (destination, *destination.parents):
            if candidate == root:
                break
            if candidate.is_symlink():
                raise ValueError("Runtime destination contains a symlink")
        if not destination.is_file() or hashlib.sha256(destination.read_bytes()).hexdigest() != entry["old_sha256"]:
            raise ValueError("Original image runtime differs from the base commit: " + str(relative))
        destinations.append(destination)
    for destination, entry in zip(destinations, entries):
        fd, temporary = tempfile.mkstemp(prefix=".vaultboot-overlay-", dir=destination.parent)
        try:
            with os.fdopen(fd, "wb") as out:
                out.write(entry["content"])
                out.flush()
                os.fsync(out.fileno())
            os.chmod(temporary, 0o644)
            os.replace(temporary, destination)
        finally:
            Path(temporary).unlink(missing_ok=True)
    return {"base_image_build_commit": base, "runtime_overlay_commit": final, "effective_pi_packages_unchanged": True,
            "changed_files": [{key: value for key, value in entry.items() if key != "content"} for entry in entries]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--base-commit", required=True)
    parser.add_argument("--final-commit", required=True)
    parser.add_argument("--manifest", required=True, type=Path)
    args = parser.parse_args()
    project = Path(__file__).resolve().parents[1]
    result = apply(project, args.root, args.base_commit, args.final_commit)
    with args.manifest.open("x") as out:
        json.dump(result, out, indent=2)
        out.write("\n")


if __name__ == "__main__":
    main()
