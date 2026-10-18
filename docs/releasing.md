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
5. Record what was tested, the environment, date, source commit, image hashes,
   limitations, and any hardware tests. Do not call an untested image stable.
6. Publish the image, ISO, package manifest, all corresponding source parts,
   source reconstruction instructions, test report, and `SHA256SUMS` together.
   Recheck checksums after downloading release assets.

The workflow's release remains a draft until reviewed. A passing build does
not prove boot or hardware compatibility. For the initial release use a
prerelease label until wider hardware testing is available.

Tag-triggered builds skip image construction if that tag already has a prepared
release, avoiding a duplicate build when uploading a locally tested candidate.
A manual workflow run always builds a fresh candidate.
