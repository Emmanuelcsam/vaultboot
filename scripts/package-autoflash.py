#!/usr/bin/env python3
"""Package the flasher with the same offline reader and pins as the OS image."""
import argparse
from pathlib import Path
import re
import tarfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path, help='new .tar.gz archive')
    parser.add_argument('--version', required=True)
    args = parser.parse_args()
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}', args.version):
        parser.error('invalid version')
    root = Path(__file__).resolve().parents[1]
    paths = ('autoflash.sh', 'scripts/autoflash.py',
             'config/includes.chroot/usr/local/lib/vaultboot/preload.py',
             'config/includes.chroot/usr/share/vaultboot/obsidian-offline.json',
             'docs/flashing.md', 'LICENSE')
    with args.output.open('xb') as output, tarfile.open(fileobj=output, mode='w:gz') as archive:
        for name in paths:
            archive.add(root / name, arcname=f'vaultboot-autoflash-{args.version}/{name}', recursive=False)


if __name__ == '__main__':
    main()
