#!/usr/bin/env python3
"""Apply the declared offline-preload files to a verified private Pi image copy.

This is a build optimization for the preview.2 -> preview.3 transition. Package
selection, source resolution and the original Pi build remain fixed. New root
files and the service enablement are recorded alongside hashes of changed files.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile

INCLUDES = "config/includes.chroot/"
ALLOWED = (
    INCLUDES + "usr/local/lib/vaultboot/runtime.py",
    INCLUDES + "usr/local/lib/vaultboot/install.py",
    INCLUDES + "usr/local/lib/vaultboot/preload.py",
    INCLUDES + "usr/local/lib/vaultboot/bootpreload.py",
    INCLUDES + "usr/share/vaultboot/obsidian-offline.json",
    INCLUDES + "etc/systemd/system/vaultboot-preload.service",
)
HOOK = "config/hooks/live/0100-vaultboot.hook.chroot"
UNCHANGED = ("config/package-lists/vaultboot.list.chroot", "build/pi-sources.py", "build/source-selection.py")
SLOT_APPEND = (b"# The finished root filesystem keeps its full 8 GiB-image extent. The new raw\n"
               b"# slot is appended outside it, with no filesystem mounting or repartitioning.\n"
               b'python3 "$project/build/preload-layout.py" append-pi "$image" > "$work/offline-layout.json"\n')
BUILD_LAYOUT = ("build/pi.sh", "build/pi-layout.py", "build/preload-layout.py")
SERVICE_LINK = "etc/systemd/system/graphical.target.wants/vaultboot-preload.service"
SERVICE_TARGET = "../vaultboot-preload.service"


def git(project, *args):
    return subprocess.check_output(["git", "-C", str(project), *args], stderr=subprocess.PIPE)


def blob(project, commit, name):
    tree = git(project, "ls-tree", commit, "--", name)
    if not tree:
        return None
    if not tree.startswith(b"100644 blob "):
        raise ValueError("Overlay files must be ordinary mode-0644 files: " + name)
    return git(project, "show", f"{commit}:{name}")


def digest(data):
    return hashlib.sha256(data).hexdigest() if data is not None else None


def plan(project, base, final):
    if not all(re.fullmatch(r"[a-f0-9]{40}", value) for value in (base, final)):
        raise ValueError("Exact git commit IDs are required")
    if git(project, "rev-parse", "HEAD").decode().strip() != final:
        raise ValueError("Checkout does not match the final source commit")
    if git(project, "status", "--porcelain", "--untracked-files=no"):
        raise ValueError("Tracked checkout files are dirty")
    if git(project, "diff", "--name-only", base, final, "--", *UNCHANGED):
        raise ValueError("Package/source selection changed; a full Pi build is required")
    for name in BUILD_LAYOUT:
        git(project, "cat-file", "-e", f"{final}:{name}")
    old_build = git(project, "show", f"{base}:build/pi.sh")
    new_build = git(project, "show", f"{final}:build/pi.sh")
    anchor = b'python3 "$project/build/pi-layout.py" "$image" > "$output/$prefix.layout.json"\n'
    if old_build.count(anchor) != 1 or new_build != old_build.replace(anchor, SLOT_APPEND + anchor):
        raise ValueError("Pi build changes exceed the declared offline-slot append")
    old_hook = git(project, "show", f"{base}:{HOOK}")
    new_hook = git(project, "show", f"{final}:{HOOK}")
    enable = b"systemctl enable NetworkManager.service lightdm.service systemd-timesyncd.service\n"
    if old_hook.count(enable) != 1 or new_hook != old_hook.replace(enable, enable.rstrip(b"\n") + b" vaultboot-preload.service\n"):
        raise ValueError("Hook changes exceed enabling the preload service")
    changed = git(project, "diff", "--name-only", "--no-renames", base, final, "--", "config").decode().splitlines()
    if set(changed) != set(ALLOWED) | {HOOK}:
        raise ValueError("Config changes must be exactly the declared offline-preload files and hook")
    entries = []
    for name in ALLOWED:
        old, new = blob(project, base, name), blob(project, final, name)
        if new is None:
            raise ValueError("Removing an overlay file is not supported")
        entries.append({"path": name, "old_sha256": digest(old), "new_sha256": digest(new), "content": new})
    unit = entries[-1]["content"].decode()
    if "\n[Install]\nWantedBy=graphical.target\n" not in unit:
        raise ValueError("Preload service enablement does not match the declared graphical target")
    return entries


def checked_path(root, relative, allow_missing=False):
    destination = root / relative
    for candidate in (destination, *destination.parents):
        if candidate == root:
            break
        if candidate.is_symlink():
            raise ValueError("Overlay destination contains a symlink")
        if candidate != destination and candidate.exists() and not candidate.is_dir():
            raise ValueError("Overlay destination parent is not a directory")
    if not allow_missing and not destination.exists():
        raise ValueError("Original image file is missing")
    return destination


def apply(project, root, base, final):
    entries = plan(project, base, final)
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise ValueError("Image root must be an existing ordinary directory")
    root = root.resolve(strict=True)
    destinations = []
    for entry in entries:
        destination = checked_path(root, entry["path"].removeprefix(INCLUDES), allow_missing=entry["old_sha256"] is None)
        if entry["old_sha256"] is None:
            if destination.exists():
                raise ValueError("New overlay destination already exists")
        else:
            if (not stat.S_ISREG(destination.stat().st_mode)
                    or stat.S_IMODE(destination.stat().st_mode) != 0o644
                    or digest(destination.read_bytes()) != entry["old_sha256"]):
                raise ValueError("Original image file differs from the base commit: " + str(destination))
        destinations.append(destination)
    service_link = checked_path(root, SERVICE_LINK, allow_missing=True)
    if service_link.exists():
        raise ValueError("Preload service was already enabled in the base image")
    for destination, entry in zip(destinations, entries):
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        fd, temporary = tempfile.mkstemp(prefix=".vaultboot-preload-", dir=destination.parent)
        try:
            with os.fdopen(fd, "wb") as out:
                out.write(entry["content"])
                out.flush()
                os.fsync(out.fileno())
            os.chmod(temporary, 0o644)
            os.replace(temporary, destination)
        finally:
            Path(temporary).unlink(missing_ok=True)
    service_link.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    service_link.symlink_to(SERVICE_TARGET)
    build_changes = []
    for name in BUILD_LAYOUT:
        old_tree = git(project, "ls-tree", base, "--", name)
        old = git(project, "show", f"{base}:{name}") if old_tree else None
        new = git(project, "show", f"{final}:{name}")
        build_changes.append({"path": name, "old_sha256": digest(old), "new_sha256": digest(new)})
    return {"base_project_commit": base, "preload_overlay_commit": final, "effective_pi_packages_unchanged": True,
            "changed_files": [{key: value for key, value in entry.items() if key != "content"} for entry in entries],
            "enabled_service": {"path": SERVICE_LINK, "target": SERVICE_TARGET}, "layout_build_changes": build_changes,
            "hook": {"path": HOOK, "old_sha256": digest(git(project, "show", f"{base}:{HOOK}")),
                     "new_sha256": digest(git(project, "show", f"{final}:{HOOK}"))}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--base-commit", required=True)
    parser.add_argument("--final-commit", required=True)
    parser.add_argument("--manifest", required=True, type=Path)
    args = parser.parse_args()
    if os.path.lexists(args.manifest):
        parser.error("Manifest path already exists")
    result = apply(Path(__file__).resolve().parents[1], args.root, args.base_commit, args.final_commit)
    with args.manifest.open("x") as output:
        json.dump(result, output, indent=2)
        output.write("\n")


if __name__ == "__main__":
    main()
