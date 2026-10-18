# Install Vaultboot on a PC's internal disk

The amd64 live USB includes an **offline installer for a normal, writable Debian
system**. After installation the PC boots directly from its internal disk. It
does not need the USB or a live persistence overlay. The installer is for amd64
PCs with legacy BIOS or 64-bit UEFI; it is not for Raspberry Pi, ARM PCs, or
32-bit-only firmware. Keep Secure Boot disabled.

**Installation erases the entire selected disk, including its operating system,
partitions and files.** Back up that disk before starting. This preview does not
resize existing partitions, provide dual boot, or encrypt the installed disk.
Use an unused writable disk of at least **16 GiB**. Disconnect other backup/data
drives when practical so there are fewer disks to confuse with the target.

1. Flash and boot the current **amd64 USB image**. First confirm its display,
   keyboard and networking work on the PC. Hardware support is unchanged by
   installation.
2. Press `Ctrl+Alt+S` and choose **Install to internal drive**. Alternatively,
   press `Ctrl+Alt+T` and run `sudo vaultboot-install`.
3. Read the disk list, including device path, size, model and serial number.
   The live USB, mounted disks, active swap and storage used by mapped volumes
   are unavailable. If your intended disk is unavailable, cancel; do not guess
   another disk. A mounted data disk must be safely unmounted before restarting
   the installer. Active swap or volume mappings require separate maintenance.
4. Select the intended disk number. Type the displayed **disk-specific erase
   line** exactly. Entering a blank selection or a mismatched line cancels before
   erasing. There is no `--yes` or unattended erase option.
5. Wait for **Installation complete**. Shut down through the system menu, remove
   the USB, then start the PC. Select the installed disk in the firmware boot
   menu if necessary. Both BIOS GRUB and the UEFI fallback loader are installed;
   the installer does not change firmware boot variables.

The installed system has a **fresh home**. Notes, cached Obsidian downloads,
Wi-Fi passwords, plugins and other changes on the live USB are **not copied**.
They remain on that USB. Restore your backed-up vault separately, including its
hidden `.obsidian` folder if you want its settings. The base Debian installation
needs no network; downloading Obsidian on the new system still needs internet.

The account remains `writer`, initially with password `live`, automatic graphical
login and passwordless local sudo. Change the password with `passwd`. Anyone with
access to the unlocked desktop can read its unencrypted notes and administer it.
Keyboard and time-zone defaults are fresh; configure them as described in
[maintenance](maintenance.md#first-run-settings).

An interruption after erasing starts can leave the selected disk incomplete.
Keep the live USB, boot it again and rerun the installer after checking the disk
identity. Other disks must remain connected until the running system shuts down
cleanly; do not hot-unplug disks during installation. The installer never resumes
an interrupted erase automatically.

Installed filesystems use UUIDs in `/etc/fstab`, and the installed GRUB entries
use ordinary root boot parameters. Live boot/configuration packages are removed.
The internal root and `/boot` are writable, so Debian kernel updates can update
the installed boot files. Continue to back up notes before system upgrades.

The bootloader options follow Debian's [GRUB installation documentation](https://manpages.debian.org/trixie/grub2-common/grub-install.8.en.html):
`--no-nvram` avoids firmware-variable writes and `--removable` installs the UEFI
fallback path. Firmware and physical-hardware compatibility still require testing
on the intended PC.
