#!/usr/bin/env python3
"""Download and verify exact Debian sources for an already configured Pi root."""

import argparse
import hashlib
import importlib.util
from pathlib import Path
import re
import subprocess


def verify_dsc_files(directory):
    """A .dsc alone is insufficient: verify every referenced source file."""
    count = 0
    for dsc in directory.glob("*.dsc"):
        data = dsc.read_text(encoding="utf-8")
        section = re.search(r"^Checksums-Sha256:\n((?: [^\n]+\n)+)", data, re.M)
        if not section:
            raise ValueError(f"No SHA256 source manifest in {dsc.name}")
        for line in section.group(1).splitlines():
            digest, size, name = line.split()
            if Path(name).name != name or not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise ValueError(f"Unsafe source filename or hash in {dsc.name}")
            path = directory / name
            if path.is_symlink() or not path.is_file() or path.stat().st_size != int(size):
                raise ValueError(f"Missing or truncated corresponding source: {name}")
            with path.open("rb") as stream:
                actual = hashlib.file_digest(stream, "sha256").hexdigest()
            if actual != digest:
                raise ValueError(f"Source checksum mismatch: {name}")
        count += 1
    if not count:
        raise ValueError("No source control files were downloaded")
    return count


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chroot", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if args.output.exists() or args.output.is_symlink():
        parser.error("Source output must be a new directory")
    args.output.mkdir()
    source = args.output / "debian"
    source.mkdir()
    apt = args.output / "apt"
    (apt / "lists/partial").mkdir(parents=True)
    (apt / "archives/partial").mkdir(parents=True)
    (apt / "status").touch()
    (apt / "sources.list").write_text(
        "deb-src https://deb.debian.org/debian trixie main contrib non-free-firmware\n"
        "deb-src https://deb.debian.org/debian trixie-updates main contrib non-free-firmware\n"
        "deb-src https://security.debian.org/debian-security trixie-security main contrib non-free-firmware\n",
        encoding="utf-8",
    )
    spec = importlib.util.spec_from_file_location("source_selection", Path(__file__).with_name("source-selection.py"))
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    records = subprocess.check_output([
        "chroot", str(args.chroot), "dpkg-query", "-W",
        "-f=${source:Package}=${source:Version}\t${Built-Using}\t${Static-Built-Using}\n",
    ], text=True)
    selections = helper.collect_sources(records)
    (args.output / "source-selection.txt").write_text("\n".join(selections) + "\n", encoding="utf-8")
    command = ["apt-get", "-o", "APT::Architecture=arm64", "-o", "Acquire::Retries=3",
               "-o", f"Dir::Etc::sourcelist={apt / 'sources.list'}",
               "-o", "Dir::Etc::sourceparts=-", "-o", f"Dir::State::lists={apt / 'lists'}",
               "-o", f"Dir::State::status={apt / 'status'}",
               "-o", f"Dir::Cache::archives={apt / 'archives'}"]
    subprocess.run(command + ["update"], check=True)
    for selection in selections:
        print(f"Corresponding source: {selection}", flush=True)
        subprocess.run(command + ["--only-source", "--download-only", "source", selection], cwd=source, check=True)
    helper.verify_sources(selections, source)
    count = verify_dsc_files(source)
    print(f"Verified {len(selections)} exact selections and all files in {count} source packages.", flush=True)


if __name__ == "__main__":
    main()
