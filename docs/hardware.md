# Hardware and boot compatibility

Vaultboot needs an image built for the processor and the device's boot firmware.
The release test report identifies the environments actually tested. One image
cannot boot every PC, Raspberry Pi, or other ARM device.

| Device | Image / current status | Requirements and limits |
| --- | --- | --- |
| Intel/AMD x86-64 PC, legacy BIOS or 64-bit UEFI | `amd64` preview | USB boot; Secure Boot disabled; working Linux drivers. See the release test report. |
| Chromebook already configured to boot ordinary Linux | Same `amd64` image if its CPU is x86-64 | Use its existing Linux boot method. Model-specific touch, sound, sleep, and special keys still need testing. |
| Raspberry Pi 3 Model B / B+, 1 GB | Separate **experimental ARM64 preview; physical hardware unverified** | Pi-specific boot files and 64-bit userspace; use a 16 GB or larger microSD. Memory is limited. |
| Raspberry Pi 3 Model A+, 512 MB | Untested | No claim that Obsidian is usable within its smaller memory. |
| Other ARM devices, ARM Chromebooks, Apple Silicon | No supported image currently | An ARM64 application alone does not supply device-specific boot support or drivers. |
| 32-bit-only PCs or PCs requiring 32-bit UEFI | Unsupported by the current image | The PC image and Obsidian package are 64-bit. |

## PC practical limits

The PC recommendation is 4 GB RAM, an 8 GB or larger USB drive, and a keyboard
and mouse or touchpad. Obsidian's own Electron runtime and plugins still use
memory; removing the desktop does not eliminate those costs. A USB SSD will
usually be more suitable for sustained writing than a slow flash drive.

The preview has no on-screen keyboard or automatic screen-rotation controls.
Use convertible laptops in laptop mode. Set a non-US
[keyboard layout](maintenance.md#first-run-settings) before entering Wi-Fi
passwords. If Wi-Fi is unsupported, a compatible USB Ethernet adapter can
provide setup connectivity on computers without an Ethernet socket.

## Raspberry Pi 3

Pi 3 Model B and B+ have 64-bit Cortex-A53 processors and 1 GB RAM; Model A+
has 512 MB. Raspberry Pi supports a 64-bit OS on this family, and Obsidian
publishes Linux ARM64 downloads. The separate Pi image needs ARM64 userspace
and Pi-specific boot support; the `amd64` image cannot boot a Pi.
[Raspberry Pi specifications](https://www.raspberrypi.com/documentation/computers/sbcs.html),
[64-bit OS support](https://www.raspberrypi.com/news/raspberry-pi-os-64-bit/),
[Obsidian downloads](https://obsidian.md/download).

**The Pi image is experimental and has not been tested on a physical Pi.** The
release test report separates image checks, emulation, and actual hardware testing.
Obsidian, large vaults, and plugins may exhaust 1 GB. Smooth operation has not
been established; begin evaluation with a small vault and no plugins. The
512 MB Model A+ remains untested.

The initial Pi route uses **microSD**, avoiding boot-firmware changes. Pi 3B
needs an irreversible one-time-programmable setting for direct USB host boot;
3B+ supports USB mass-storage boot out of the box. Older Pi models have USB
device compatibility and power constraints. Vaultboot does not change OTP
settings automatically.
[Raspberry Pi USB boot documentation](https://www.raspberrypi.com/documentation/computers/raspberry-pi.html#usb-mass-storage-boot).
