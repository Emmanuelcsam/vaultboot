# Architecture

```text
PC BIOS / UEFI
    -> Debian live bootloader, kernel and initramfs
    -> read-only SquashFS + writable ext4 persistence overlay
    -> LightDM automatic local sign-in (writer)
    -> Xorg + Openbox (no desktop panel or application suite)
    -> Vaultboot session supervisor
    -> Obsidian, with a normal user account and Electron sandbox
```

The first session installs Obsidian into the user's data directory from its
official upstream release. The installer is separate from the OS build, so
the public image contains no Obsidian binary. A tiny system menu provides the
necessary network, recovery, update, and power controls. Standard Debian tools
remain accessible for maintenance.

The app installer uses a per-user lock, validates release metadata and the
download digest, stages extraction, and changes the current installation only
after success. Network failures remain visible and retryable. The session
supervisor restarts the application with a delay and surfaces repeated crashes.

The public image is a personal single-user environment. Obsidian plugins run as
that user and can read their files. This is not an untrusted-plugin sandbox or
multi-tenant system. No plugin is enabled by default.

`build/` and `scripts/` produce the image. `config/includes.chroot/` becomes the
image's filesystem overlay; `config/package-lists/` specifies Debian packages.
Unit tests isolate downloads and home directories. Actual image boot checks
must also cover BIOS, UEFI, application launch, and persistence across reboot.

Builders use stable Debian package repositories at build time. The release
manifest records installed versions and matching sources accompany binaries.
Rebuilds against changing mirrors are not claimed to be bit-for-bit reproducible.
