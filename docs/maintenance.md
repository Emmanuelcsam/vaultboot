# Storage and maintenance

## Persistence

Use the persistent `vaultboot-…-amd64.img.gz` release for daily work. It contains the live
system and an ext4 volume labelled `vaultboot-data`, with a `persistence.conf`
file containing `/ union`. Debian live-boot overlays this writable storage on
the base system. Notes, the downloaded Obsidian app, system changes, and Wi-Fi
configuration survive reboot.

The separate `pi3-arm64.img.gz` uses an ordinary writable ext4 root filesystem
on microSD. It does not use the PC live overlay or its persistence label. Its
notes, application, settings, and network configuration are saved there.

## First boot and Wi-Fi

An AUTOFLASH-prepared drive opens Obsidian offline on first boot. Use
`Ctrl+Alt+S` whenever you want to connect Wi-Fi or change system settings.
On unprepared media, setup offers **Keyboard layout**, **Wi-Fi / Ethernet**,
and **Time zone** before the first Obsidian download. Choose your keyboard
layout before typing a Wi-Fi
password. The same controls remain available through `Ctrl+Alt+S`.

Choose **Wi-Fi / Ethernet → Connect to Wi-Fi / Ethernet**. Use the arrow keys to
select your network, `Tab` to move between controls, and `Enter` to connect.
Enter the password when asked, then choose **Back**. Close the network status
message. If app setup is still pending, choose **Download Obsidian**. Ethernet
normally connects
automatically. **Edit saved connections** provides advanced settings, including
hidden networks and manual addressing.

NetworkManager saves connection profiles and normally reconnects on later
boots. Saving requires persistent storage. **Turn Wi-Fi on** can re-enable a
disabled radio; a physical airplane-mode switch must also be enabled. The
network window reports detected adapters and connection state. A connected
adapter does not prove the internet is reachable: if the download fails, fix
the connection and retry. An already installed Obsidian starts offline.

A network requiring a web sign-in page needs a browser, which this image does
not include. Use home Wi-Fi, wired Ethernet, or a phone hotspot for setup.
Firmware for common adapters is included, but driver support depends on the
actual hardware. A missing Wi-Fi device may need a supported USB adapter or
Ethernet. VM networking checks do not certify a physical laptop's radio.

The network interface is NetworkManager's [nmtui connection list](https://networkmanager.pages.freedesktop.org/NetworkManager/NetworkManager/nmtui.html).

## Everyday controls

Press **Ctrl+Alt+S** for the system menu. It scrolls on smaller displays.

| Control | What it does |
| --- | --- |
| Files and attached drives | Opens the Notes folder in PCManFM. Attached USB drives appear in its sidebar; click one to mount it. Use its eject/unmount control before unplugging a data drive. Ctrl+Alt+F is the direct shortcut. |
| Sound / headphones | Opens volume, output-device, headphone-port and microphone controls. Standard mute/volume keys also control the default output. ALSA's mixer is the fallback if the graphical sound tool is unavailable. |
| Display resolution | Lists connected monitors and their supported modes. Keep the chosen mode within 15 seconds or it reverts. A confirmed mode is saved and reused only when the matching output still offers it. |
| Screen brightness | Adjusts a supported laptop backlight, with a minimum of 5%. External monitors usually require their physical buttons. |
| Keyboard layout | Selects a layout and variant for the graphical session, and saves it for later boots. |
| Time zone | Selects a Region/City timezone. Network time synchronization is enabled by the image when internet access is available. |
| Storage / battery / status | Shows writable free space, persistent/temporary storage, battery or external power when reported, network devices, app version and log locations. |
| Backup Notes vault | Saves a compressed backup of the default Notes vault, including hidden settings, to a folder you choose. Mount the destination drive in Files first. |
| Suspend | Asks you to save before sleeping. Keep the boot USB connected. Resume support depends on the computer and its firmware. |
| Restart computer / Shut down | Confirms the action before stopping the session. |

Display, brightness and keyboard preferences live in
`~/.local/share/vaultboot/settings.json`; they survive reboot on persistent
storage. If a graphics preference causes trouble, remove its `display` entry
from that file in a maintenance terminal, then restart. `xrandr --auto` asks the
driver for its preferred current resolution. Multiple-monitor positioning is
an advanced `xrandr` task; the menu changes resolution only.

The keyboard menu controls the graphical session. For text-console layouts,
use `sudo dpkg-reconfigure keyboard-configuration`. Unsupported battery,
backlight or suspend hardware is reported without preventing note-taking.
These controls use Debian's existing device services; no desktop suite or
browser is installed. File management and removable-media support come from
[PCManFM](https://packages.debian.org/trixie/pcmanfm) and GVFS/udisks2.

## Available space

Choose **Storage / battery / status**, or check free space in a maintenance terminal:

```sh
df -h / /home/writer
```

The PC image includes 4 GiB of writable storage in addition to the compressed OS.
The downloaded app uses some of it. Large vaults or package upgrades may fill
the remainder. Use a larger external data
drive for large attachments, or expand the persistence partition offline using
a partition editor on another Linux system after backing it up. Partition
editing is an advanced task; the system never automatically reformats or grows
your disks.

The Pi image includes an 8 GiB firmware/root layout plus a 256 MiB
offline-preparation partition. The OS, application, and notes share the root
filesystem. A larger
microSD does not automatically grow the filesystem in this preview.

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

Choose **Update Debian packages** in the system menu to open a terminal that
refreshes package lists and asks you to review the proposed upgrades. It does
not accept package changes automatically. The equivalent maintenance commands
are:

```sh
sudo apt update
sudo apt upgrade
```

Review proposed package changes and available storage. On the PC live USB the
boot kernel/initrd live in the read-only image: a package upgrade does **not**
replace those boot files. For kernel fixes or a new Vaultboot live base, back up
your data, flash the new release, and restore your vaults. An internal-drive
installation has a normal writable root and `/boot`; Debian package upgrades
can update its boot kernel normally. Pi boot files are writable, but kernel/firmware
upgrades need hardware testing; backing up and reflashing is the supported
preview base-update procedure for both images. No seamless base-image upgrade
is claimed.

## Recovery

Use `Ctrl+Alt+S` for network setup or diagnostics and `Ctrl+Alt+T` for the terminal.
On PCs, if graphics fails, try appending `nomodeset` to the live entry's kernel command
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

## Installing on an internal drive

Boot the PC Vaultboot USB, confirm that its display and network work, and make
backups of the computer's existing files. Choose **Install to internal drive**
in setup or the system menu. The installer opens in a terminal, identifies
eligible drives, and requires a typed confirmation identifying the selected
disk before erasing it. **The selected drive's existing operating system,
partitions and files are replaced.** This is a whole-drive installation, not a
dual-boot setup.

The destination must be an unused, writable whole drive of at least 16 GiB.
The installer creates a GPT layout with a small BIOS boot partition, a 512 MiB
EFI system partition, and an ext4 root filesystem using the remaining space.
It configures BIOS and 64-bit UEFI boot without changing the machine's UEFI
boot entries. Encryption and 32-bit UEFI are not supported by this installer.

Installation creates a **fresh system** from the release image. It does not
copy the live session's notes, downloaded Obsidian app, settings, or saved Wi-Fi
profiles. Keep their backups on another drive. On the first internal-drive
boot, connect to the network, download Obsidian again, then restore your vaults
through Files. The installed system uses ordinary writable storage rather than
the USB's persistence overlay.

The menu entry appears only on a 64-bit Intel/AMD PC booted from Vaultboot live
media. It is absent on the Raspberry Pi and on an installed system without the
live installation files. The installer itself performs the safety checks; the
menu does not format disks. Read the terminal's result before closing it. Shut
down after installation, remove the installer USB, and boot the internal drive.
The separate Raspberry Pi image is flashed to microSD instead.
