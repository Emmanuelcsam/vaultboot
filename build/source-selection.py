#!/usr/bin/env python3
"""List exact Debian sources, including code incorporated into other binaries."""

import argparse
from pathlib import Path
import re
import subprocess

SOURCE = re.compile(r"[a-z0-9][a-z0-9+.-]*=[A-Za-z0-9.+:~_-]+")
INCORPORATED = re.compile(r"\s*([a-z0-9][a-z0-9+.-]*)\s*\(=\s*([A-Za-z0-9.+:~_-]+)\)\s*")


def collect_sources(records):
    sources = set()
    for record in records.splitlines():
        if not record:
            continue
        fields = record.split("\t")
        if len(fields) != 3 or not SOURCE.fullmatch(fields[0]):
            raise ValueError(f"Invalid Debian package source record: {record!r}")
        sources.add(fields[0])
        for field in fields[1:]:
            if not field.strip():
                continue
            for dependency in field.split(","):
                match = INCORPORATED.fullmatch(dependency)
                if not match:
                    raise ValueError(f"Invalid exact incorporated-source dependency: {dependency!r}")
                sources.add("=".join(match.groups()))
    if not sources:
        raise ValueError("No installed package sources were found")
    return sorted(sources)


def verify_sources(expected, directory):
    present = set()
    for path in directory.rglob("*.dsc"):
        data = path.read_text(encoding="utf-8")
        source = re.search(r"^Source: (\S+)$", data, re.M)
        version = re.search(r"^Version: (\S+)$", data, re.M)
        if source and version:
            present.add(source.group(1) + "=" + version.group(1))
    missing = sorted(set(expected) - present)
    if missing:
        raise ValueError("Missing exact corresponding-source archives: " + ", ".join(missing))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify", type=Path, help="Verify all selected exact versions have a .dsc here")
    args = parser.parse_args()
    records = subprocess.check_output([
        "chroot", "chroot", "dpkg-query", "-W",
        "-f=${source:Package}=${source:Version}\t${Built-Using}\t${Static-Built-Using}\n",
    ], text=True)
    sources = collect_sources(records)
    if args.verify:
        verify_sources(sources, args.verify)
        print(f"Verified all {len(sources)} exact source selections, including incorporated code.")
    else:
        print("\n".join(sources))


if __name__ == "__main__":
    main()
