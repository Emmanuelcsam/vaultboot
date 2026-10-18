# Flash Vaultboot to a USB drive or microSD card

Download this repository's source ZIP from GitHub and extract it, or clone the
repository. Open a terminal in that folder and run:

```sh
./autoflash.sh
```

AUTOFLASH asks for PC or Raspberry Pi, downloads the newest published image
**including preview releases**, verifies its SHA-256, and checks the complete
gzip file before touching a drive. It then asks you to accept Obsidian’s terms,
downloads the pinned official Obsidian package, and checks its exact size and
SHA-256. This prepares offline first boot for the target architecture.
It lists drives with their model, capacity,
and safety status. Type the whole drive path yourself, then type the exact
`ERASE ...` phrase shown for that drive. There is no default drive and no
unattended confirmation option.

**Everything on the selected drive will be erased.** Back up its contents
first. For less chance of choosing the wrong drive, disconnect other removable
storage. Do not unplug the chosen drive while writing or verifying. A checksum
failure or interrupted write means the drive must be flashed again.

Use an **8 GB or larger USB drive for a PC**, or a **16 GB or larger microSD
card for a Raspberry Pi 3B/3B+**. A USB card reader is the simplest way to flash
the Pi card. The image target describes the computer you will boot; it does
not need to match the computer running the flasher.

Boot with only one Vaultboot USB connected. Copies share the default persistence
label; connect other Vaultboot or backup drives after the system has started.

## Supported flashing computers

- **Native Linux:** Python 3.9 or newer, Linux 5.15 or newer, `lsblk`, `udevadm`,
  `umount`, and `sudo`. Debian/Ubuntu provide these through Python, util-linux,
  udev, and sudo packages. The script requests administrator access itself.
- **macOS:** Python 3.9 or newer, Apple's built-in `diskutil`, and an administrator
  account. Use an external USB drive or USB card reader. The macOS adapter has
  automated policy tests; a physical Mac flash has not been validated for this
  preview. Unsupported device or cache operations stop before writing.
- **Windows / WSL:** AUTOFLASH is not supported. Use the manual path below.

Linux and macOS adapters accept only whole removable/USB disks, and only
512-byte logical sectors, matching these images. Internal disks, partitions,
read-only media, system/source/staging storage, swap, and complex storage such
as RAID/LVM/encryption/APFS are excluded. Linux mounts outside `/media` and
`/run/media`, and macOS mounts outside `/Volumes`, are also excluded. If a
selected eligible drive is mounted, the script requests an ordinary unmount;
it never forces a busy filesystem off the computer. Close its files and retry
if it is busy. Unsupported readers are refused; use another reader or the
manual flasher instead of weakening these checks.

Normally unmount any NTFS/FUSE block volumes on the selected drive before
rerunning AUTOFLASH; mounted FUSE block volumes are excluded from selection.

The flasher needs local temporary space for the compressed and uncompressed
image together (roughly **7 GiB for PC or 11 GiB for Pi** for this preview).
It checks available space while staging and removes its private temporary
folder when finished. Storage requirements can change with later releases.
Choose another existing staging parent with `--cache-dir /path/to/folder`.
That folder must be on a different disk from the drive being erased.

## Useful commands

List drives without downloading, unmounting, or writing:

```sh
./autoflash.sh --list
```

Choose the PC image, or pin a specific release:

```sh
./autoflash.sh --target amd64
./autoflash.sh --target pi3-arm64 --release v0.1.0-preview.3
```

Supply a whole drive path from the listing. The typed confirmation is still
required. `/dev/sdX` below is a placeholder, not a drive to copy literally:

```sh
./autoflash.sh --target amd64 --device /dev/sdX
```

Use an image and `SHA256SUMS` already downloaded from the same official release:

```sh
./autoflash.sh --target amd64 \
  --image /path/to/vaultboot-0.1.0-preview.3-amd64.img.gz \
  --manifest /path/to/SHA256SUMS
```

Keep the original release filename. The same download, decompression, device,
and read-back checks apply to local files. The official HTTPS release and its
manifest are the trust source; SHA-256 detects corruption and mismatch, and
is not an independent signature from another publisher.

AUTOFLASH fills the image’s reserved 256 MiB preparation partition in a private
staging file, then calculates the prepared image’s SHA-256. It preserves the
boot records and all other partitions, including the Pi’s hybrid boot records.
It clears at most the last 1 MiB **outside** the image to remove stale partition
metadata from a previously used drive, then verifies that area as well. It does
not enlarge partitions or relocate the Pi's GPT during flashing. After flushing,
it reads back
all written image bytes and checks their SHA-256. Wait for the success message
before removing the drive. This is imaging, not a secure erase of all old data
in unused space.

## Offline first boot

The preparation computer needs internet to download Obsidian, even when you
supply an already downloaded OS image with `--image`. The destination does not:
the prepared USB or microSD carries the verified package. The PC installer
carries this same package into a fresh internal-disk installation.

The public image contains an empty preparation partition. Only AUTOFLASH fills
it with your copy, directly from the publisher, after you accept its terms.
Use the flasher from the same release as your image. Images older than preview.3
have no preparation partition and are rejected by the default preparation path.

To skip preparation deliberately:

```sh
./autoflash.sh --target amd64 --no-preload
```

This still verifies and flashes the OS, but the destination needs internet for
its first Obsidian download. Never bypass a corrupt preparation warning: retry
with the matching release, or explicitly choose the online setup path.

## Manual flashing on Windows, or with a graphical tool

1. Open the [Vaultboot releases](https://github.com/Emmanuelcsam/vaultboot/releases)
   and download the matching `amd64.img.gz` or `pi3-arm64.img.gz` plus `SHA256SUMS`.
2. On Windows, run `Get-FileHash .\vaultboot-VERSION-TARGET.img.gz -Algorithm SHA256`
   in PowerShell and compare the full result with that filename's line in
   `SHA256SUMS`. On Linux use `sha256sum`; on macOS use `shasum -a 256`.
3. Use [balenaEtcher](https://etcher.balena.io/) to select the image, explicitly
   choose the USB drive or microSD card, and flash it. Etcher accepts compressed
   images. Let its validation finish, then safely eject the drive. If your tool
   requires an uncompressed image, extract the `.gz` first and select `.img`.
4. Boot the PC from USB, or put the card into the Pi. PC firmware requirements
   and the experimental Pi hardware status are in [hardware.md](hardware.md).

Manual flashing leaves the preparation partition empty. Connect the destination
to the internet for its first Obsidian download. Offline preparation currently
requires AUTOFLASH on native Linux or macOS.

The optional PC internal-disk installer is available in
**v0.1.0-preview.2 and later**. A USB made from preview.1 needs reflashing to gain
that installer. Flashing removable media itself does not install onto a PC's
internal disk. Raspberry Pi uses its microSD image directly.
