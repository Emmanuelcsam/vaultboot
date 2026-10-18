# Storage and maintenance

## Persistence

Use the persistent `vaultboot-…-amd64.img.gz` release for daily work. It contains the live
system and an ext4 volume labelled `vaultboot-data`, with a `persistence.conf`
file containing `/ union`. Debian live-boot overlays this writable storage on
the base system. Notes, the downloaded Obsidian app, system changes, and Wi-Fi
configuration survive reboot.

## First-run settings

The preview starts with a US English keyboard layout. If your keyboard uses
another layout, press `Ctrl+Alt+T` and set it before entering a Wi-Fi password.
For example, this selects a UK layout for the current graphical session:

```sh
setxkbmap gb
```

Use your layout code in place of `gb`, such as `de`, `fr`, or `es`. To configure
the saved default, run `sudo dpkg-reconfigure keyboard-configuration`, choose
your keyboard/layout, then restart through the system menu. The saved setting
requires persistent storage. Set your time zone with `sudo dpkg-reconfigure tzdata`
if the default UTC time is unsuitable.

For Wi-Fi, choose **Connect Wi-Fi / network** in setup, then **Activate a
connection**. Use arrow keys to select your network, `Tab` to move between
controls, and `Enter` to activate it. Enter the Wi-Fi password, then use `Back`
and `Quit` to return to setup and choose **Download Obsidian**. A network that
requires signing in through a web page needs another connection for setup,
because this image has no browser. A wired Ethernet connection is simplest
when an old Wi-Fi adapter is unsupported.

## Available space

Check free space in a maintenance terminal:

```sh
df -h / /home/writer
```

The image includes 4 GiB of writable storage in addition to the compressed OS.
The downloaded app uses some of it. Large vaults or package upgrades may fill
the remainder. Use a larger external data
drive for large attachments, or expand the persistence partition offline using
a partition editor on another Linux system after backing it up. Partition
editing is an advanced task; the system never automatically reformats or grows
your disks.

Do not have two Vaultboot USBs attached during startup: they share the
persistence label, so the wrong writable volume could be selected. Shut down
cleanly before moving a drive to another computer.

## Backups and updates

Copy every vault to separate storage regularly. Include hidden `.obsidian`
folders if you want to preserve plugins/settings. Keep a second copy of files
before changing plugins, partitions, or the operating system.

Use the system menu's Obsidian update action for an upstream installer update.
Downloads are verified and promoted only after successful extraction; a failed
download leaves the existing app available. Obsidian can also download its own
application updates; occasionally a newer Electron runtime requires updating
the installer through the system menu.

Debian package updates are available from the maintenance terminal:

```sh
sudo apt update
sudo apt upgrade
```

Review proposed package changes and available storage. The boot kernel/initrd
live in the read-only image: a package upgrade does **not** replace those boot
files. For kernel fixes or a new Vaultboot base, back up your data, flash the new
release, and restore your vaults. No seamless base-image upgrade is claimed.

## Recovery

Use `Ctrl+Alt+S` for network setup or diagnostics and `Ctrl+Alt+T` for the terminal.
If graphics fails, try appending `nomodeset` to the live entry's kernel command
line in the bootloader menu. In UEFI's GRUB menu press `e`, edit the line
beginning with `linux`, then press `Ctrl+X` to boot. In the BIOS boot menu,
press `Tab` on the live entry to edit its options, then `Enter` to boot. This
change applies only to that boot; hardware GPU support varies.
A text console is available with `Ctrl+Alt+F2` (account `writer`,
password `live`). Return to graphics with `Ctrl+Alt+F7`, or the active display VT.

Logs are kept under `~/.local/state/vaultboot`. They may contain local file names;
review them before posting publicly. If Obsidian stops working after enabling a
plugin, move the affected vault's `.obsidian/plugins` directory aside with
Obsidian closed, then restart it. Keep the directory as a backup.

## Using an internal disk

The same raw image can be written to an internal disk from another booted
operating system, making the PC boot Vaultboot without the USB. **This replaces
the selected disk's existing operating system and data.** There is no automatic
internal-disk installer in this preview. First confirm the USB works with your
hardware, make backups, and use an image writer that clearly identifies the
target disk. Do not write over the disk from which you are currently running.
