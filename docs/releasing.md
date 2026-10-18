# Release procedure

1. Run unit tests and shell syntax checks. Review all files for secrets and
   accidental personal data. Never bundle a personal vault, browser profile,
   SSH key, token, or private log.
2. Build in an isolated Linux x86-64 environment. The builder needs privileged
   container support for chroot mounts, and substantial free disk space for
   the binaries and full corresponding Debian sources.
3. Boot the exact candidate image in both BIOS and UEFI virtual machines.
   Verify automatic login, first-run network setup, upstream installation,
   Obsidian launch as the normal local user, system menu, and clean shutdown.
4. In a writable USB-image VM, create a note, reboot, and verify it survives
   alongside the installed app. Boot with networking disabled and verify the
   cached app opens. Confirm the ISO warns about volatile storage.
   Exercise the default autoflasher against a disposable emulated USB disk and
   verify its package download, terms receipt, and complete write/read-back path.
   Cold-boot that fresh prepared drive with no network device and no seeded
   user cache; verify Obsidian opens. Repeat for the ARM64 software image. Test refusals for system/source disks,
   partitions, in-use targets, changed identity, bad checksums, and truncation.
   Install to a separate disposable virtual disk, remove the live USB, and boot
   the installed disk independently in both BIOS and UEFI modes. Verify saved
   notes after reboot. The first installed boot must open Obsidian with no
   network device and no USB attached, using only the installer-carried package.
   Never use a developer's physical disk for these checks.
5. Record what was tested, the environment, date, source commit, image hashes,
   limitations, and any hardware tests. Do not call an untested image stable.
6. Build the standalone AUTOFLASH bundle with `scripts/package-autoflash.py`,
   retaining the canonical shared module and pins. Confirm public image slots
   are empty; never publish a prepared image containing Obsidian.
7. Publish the image, ISO, AUTOFLASH bundle, package manifest, all corresponding source parts,
   source reconstruction instructions, test report, and `SHA256SUMS` together.
   Recheck checksums after downloading release assets.

The workflow's release remains a draft until reviewed. A passing build does
not prove boot or hardware compatibility. For the initial release use a
prerelease label until wider hardware testing is available.

Tag-triggered builds skip image construction if that tag already has a prepared
release, avoiding a duplicate build when uploading a locally tested candidate.
A manual workflow run always builds a fresh candidate.
