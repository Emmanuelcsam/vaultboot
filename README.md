# Vaultboot

**Turn a PC into a dedicated Obsidian notebook.**

Vaultboot is a small Debian 13 system that signs in automatically and opens
Obsidian. There is no desktop panel, app launcher, office suite, or web browser.
Debian handles the hardware, networking, sound, and storage underneath.

This is an independent community project, not an official Obsidian product.
Obsidian itself is downloaded from its official releases during first setup;
it is not redistributed in our images. After setup, your app, settings, and
notes stay on the USB drive and work offline.

**Early preview:** see the release's test report for what has actually been
tested. Compatibility with your particular PC is not guaranteed.

## Download and boot

1. Open [Releases](https://github.com/Emmanuelcsam/vaultboot/releases) and download
   the **`vaultboot-…-amd64.img.gz`** file and `SHA256SUMS`. The source archives are for developers;
   you do not need them to use the system.
2. Verify the download against `SHA256SUMS`, then extract the `.gz` file to get
   the `.img`. The raw image is about 4.7 GiB; allow at least 8 GiB free space
   for extraction.
3. Use an image writer such as [Raspberry Pi Imager](https://www.raspberrypi.com/software/)
   (choose a custom image) or [balenaEtcher](https://etcher.balena.io/) to write the
   `.img` to an **8 GB or larger USB drive**. Check the selected drive carefully:
   flashing erases that drive.
4. Boot the PC from USB using its firmware boot menu. Turn off Secure Boot for
   this preview. Both legacy BIOS and 64-bit UEFI boot are supported by the image.
5. Connect to the internet for the one-time Obsidian download. Ethernet normally
   connects automatically; the setup screen can open Wi-Fi settings.
6. Open your notebook. Subsequent boots go straight into Obsidian.

The USB image includes a writable partition for notes and settings. It boots
without installing to or erasing the PC's internal drive. Leave the USB plugged
in while using it. Use the system menu to shut down before removing it.
If Windows or macOS offers to format or initialize the USB after flashing,
cancel: its Linux storage is intentional.

The preview starts with a US English keyboard layout. For another layout,
follow [first-run settings](docs/maintenance.md#first-run-settings) before
entering a Wi-Fi password. That section also explains the keyboard controls
in the network setup screen.

The separate **`.iso` is a temporary demo / VM image**. Without a persistence
volume, changes disappear at shutdown; the setup screen warns about this.
Choose the USB image for everyday use.

## Hardware

- A **64-bit Intel or AMD PC** with USB boot. Not 32-bit x86, ARM, Raspberry Pi,
  or Apple Silicon. Some old 64-bit machines have 32-bit UEFI and are unsupported.
- **4 GB RAM recommended** as a project starting point, not an official Obsidian
  minimum. Plugin count, graph size, attachments, and vault size affect memory.
- An 8 GB or larger USB drive; a USB SSD is preferable for sustained use.
- Common Intel/AMD graphics and common Wi-Fi firmware are included. Unusual
  hardware may need extra drivers. Ethernet is the simplest setup connection.

Obsidian still contains Electron. Removing a desktop reduces background work,
but does not remove Obsidian's own CPU and memory needs. No benchmark against
your old operating system is claimed.

## Everyday controls

| Shortcut | Action |
| --- | --- |
| `Ctrl+Alt+S` | System menu: network, update, help, shutdown/restart |
| `Ctrl+Alt+T` | Maintenance terminal |

Your default vault is `~/Notes`. Plugins and themes work through Obsidian as
usual; none are preinstalled. The system reopens Obsidian if it exits, with
failure recovery rather than an endless fast restart loop.

There is intentionally no browser. Web links, browser-only login flows, and
plugins requiring external applications may not work. Obsidian Sync is optional
and subject to its own account/subscription terms. Do not depend on it as your
only backup.

## Keep your notes safe

This is a personal appliance with automatic sign-in, **not a locked public kiosk**.
Someone with physical access can read its unencrypted USB data and use its
maintenance tools. The live account is `writer`, password `live`, with local sudo
access. No SSH server is installed. Change the password with `passwd` if desired;
automatic graphical sign-in remains enabled.

Back up `~/Notes` and any other vaults to another drive or backup service. A
bootable USB is not a backup. The image includes a fixed 4 GiB writable partition in addition to the base OS;
a larger USB does not automatically enlarge it. See
[storage and maintenance](docs/maintenance.md) for capacity and upgrades.

**Reflashing destroys the USB's notes and settings.** Back them up first.

## Build and contribute

The project uses Debian's `live-build`, ordinary shell/Python, Xorg, Openbox,
and LightDM. There is no custom kernel, Arch base, proprietary plugin bundle,
or remote server dependency after setup.

```sh
python3 -m unittest discover -s tests -v
# Linux x86-64 host with Docker, enough disk space, and permission to use Docker:
VAULTBOOT_VERSION=0.1.0 ./scripts/build.sh ./artifacts
```

Building uses a privileged Docker container to construct a Debian chroot.
Use a trusted disposable build machine; do not give this permission to
untrusted pull requests. Runtime processes do not run in that container.
See [architecture](docs/architecture.md) and [release procedure](docs/releasing.md).

Original project code is MIT licensed. Third-party packages keep their own
licenses. Every binary release must include its corresponding Debian sources,
package manifest, and checksums; see [licensing](docs/licensing.md).
