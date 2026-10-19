#!/bin/bash
# Apply one verified installer-file fix to an already source-complete PC build.
set -Eeuo pipefail
umask 022
cd /work
[[ $(id -u) == 0 && -d /previous && -d /out && -d /src ]]
[[ -z $(find /out -mindepth 1 -maxdepth 1 -print -quit) ]]
[[ -f .build/source_debian && -f .build/source_tar && -f .build/binary_iso ]]
[[ ! -s source/missing-source.txt ]]
[[ ${OVERLAY_COMMIT:-} =~ ^[0-9a-f]{40}$ ]]
[[ ${OVERLAY_INSTALLER_SHA256:-} =~ ^[0-9a-f]{64}$ ]]
[[ ${PREVIOUS_INSTALLER_SHA256:-} =~ ^[0-9a-f]{64}$ ]]
export SOURCE_DATE_EPOCH=${SOURCE_DATE_EPOCH:?Set the original build epoch}
[[ $SOURCE_DATE_EPOCH =~ ^[0-9]+$ ]]
version=${VAULTBOOT_VERSION:-0.1.0-preview.1}
[[ $version =~ ^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$ ]]
prefix="vaultboot-${version}-amd64"
(cd /previous && sha256sum --check --quiet SHA256SUMS)
python3 /src/build/source-selection.py --verify source/debian
new_installer=/src/config/includes.chroot/usr/local/lib/vaultboot/install.py
printf '%s  %s\n' "$OVERLAY_INSTALLER_SHA256" "$new_installer" | sha256sum --check
stage=$(mktemp -d /work/installer-final.XXXXXXXX)
xorriso -osirrox on -indev "/previous/$prefix.iso" \
    -extract /live/filesystem.squashfs "$stage/original.squashfs"
unsquashfs -processors 2 -d "$stage/root" "$stage/original.squashfs"
for destination in "$stage/root/usr/local/lib/vaultboot/install.py" \
    config/includes.chroot/usr/local/lib/vaultboot/install.py chroot/usr/local/lib/vaultboot/install.py; do
    printf '%s  %s\n' "$PREVIOUS_INSTALLER_SHA256" "$destination" | sha256sum --check
done
for destination in "$stage/root/usr/local/lib/vaultboot/install.py" \
    config/includes.chroot/usr/local/lib/vaultboot/install.py chroot/usr/local/lib/vaultboot/install.py; do
    install -m 0644 "$new_installer" "$destination"
    touch -d "@$SOURCE_DATE_EPOCH" "$destination"
done
mksquashfs "$stage/root" "$stage/final.squashfs" \
    -noappend -no-progress -comp xz -b 1M -processors 2 -mem 1G
mv "$stage/final.squashfs" binary/live/filesystem.squashfs
# The installer verifies this dedicated manifest before offering any disk.
# Refresh it before the general ISO checksums, then explicitly map both.
(cd binary && sha256sum live/filesystem.squashfs > vaultboot-squashfs.sha256)
lb binary_checksums --force
checksum_maps=(-map /work/binary/vaultboot-squashfs.sha256 /vaultboot-squashfs.sha256)
for checksum in binary/*sum.txt; do
    [[ -f $checksum ]] || continue
    checksum_maps+=(-map "/work/$checksum" "/${checksum##*/}")
done
(( ${#checksum_maps[@]} > 0 ))
xorriso -indev "/previous/$prefix.iso" -outdev "/out/$prefix.iso" \
    -boot_image any replay -map /work/binary/live/filesystem.squashfs /live/filesystem.squashfs \
    "${checksum_maps[@]}" -commit -end
# Verify the bytes actually embedded in the completed ISO, not only staging.
mkdir -p "$stage/iso-check/live"
xorriso -osirrox on -indev "/out/$prefix.iso" \
    -extract /live/filesystem.squashfs "$stage/iso-check/live/filesystem.squashfs" \
    -extract /vaultboot-squashfs.sha256 "$stage/iso-check/vaultboot-squashfs.sha256"
(cd "$stage/iso-check" && sha256sum --check vaultboot-squashfs.sha256)
python3 /src/scripts/make-usb-image.py --iso "/out/$prefix.iso" \
    --output "/out/$prefix.img" --persistence-mib 4096
gzip -n -1 "/out/$prefix.img"
cp "/previous/$prefix.packages.tsv" /out/
cp /previous/*-source.debian.tar.gz.part-* /out/
lb source_live --force
lb source_checksums --force
tar cf "$stage/source.live.tar" --sort=name source/live
gzip -n -1 "$stage/source.live.tar"
split --bytes=1700M --numeric-suffixes=0 --suffix-length=3 "$stage/source.live.tar.gz" \
    "/out/$prefix-source.live.tar.gz.part-"
cp /previous/SOURCES.txt /previous/BUILD-INFO.txt /previous/LICENSE.txt /out/
python3 /src/scripts/package-autoflash.py --version "$version" "/out/vaultboot-${version}-autoflash.tar.gz"
printf 'Installer overlay commit: %s\nPrevious installer SHA256: %s\nFinal installer SHA256: %s\nOverlay: only install.py; freshly installed root mode normalized to0755; hybrid boot records replayed and exact source.live regenerated. Debian packages and source parts unchanged.\n' \
    "$OVERLAY_COMMIT" "$PREVIOUS_INSTALLER_SHA256" "$OVERLAY_INSTALLER_SHA256" >> /out/BUILD-INFO.txt
sha256sum /src/build/refinalize-installer-amd64.sh | sed 's|/src/||' >> /out/BUILD-INFO.txt
cd /out
sha256sum -- *.iso *.img.gz *.layout.json *.packages.tsv *.part-* *-autoflash.tar.gz LICENSE.txt SOURCES.txt BUILD-INFO.txt > SHA256SUMS
sha256sum --check --quiet SHA256SUMS
echo 'Installer-corrected PC artifacts complete; final-byte cold boot verification required.'
