#!/bin/bash
# Apply the single Openbox rule correction to a completed, preserved r5 workspace.
# Run in the same disposable Debian builder, with /work, /previous and /out mounted.
set -Eeuo pipefail
umask 022
cd /work
[[ $(id -u) == 0 && -d /previous && -d /out && -d /src ]]
[[ -z $(find /out -mindepth 1 -maxdepth 1 -print -quit) ]]
[[ -f .build/source_debian && -f .build/source_tar && -f .build/binary_iso ]]
[[ ! -e source/missing-source.txt && ! -e /work/vaultboot-final-root ]]
[[ ${OVERLAY_COMMIT:-} =~ ^[0-9a-f]{40}$ ]]
[[ ${OVERLAY_XML_SHA256:-} =~ ^[0-9a-f]{64}$ ]]
version=${VAULTBOOT_VERSION:-0.1.0-preview.1}
[[ $version =~ ^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$ ]]
prefix="vaultboot-${version}-amd64"
[[ -s /previous/$prefix.iso && -s /previous/SHA256SUMS ]]
(cd /previous && sha256sum --check SHA256SUMS)
python3 /src/build/source-selection.py --verify source/debian
new_config=/src/config/includes.chroot/etc/xdg/openbox/vaultboot-rc.xml
printf '%s  %s\n' "$OVERLAY_XML_SHA256" "$new_config" | sha256sum --check
grep -Fq 'class="md.obsidian.Obsidian"' "$new_config"
export SOURCE_DATE_EPOCH=${SOURCE_DATE_EPOCH:?Set the original r5 SOURCE_DATE_EPOCH}
[[ $SOURCE_DATE_EPOCH =~ ^[0-9]+$ ]]
unsquashfs -processors 2 -d /work/vaultboot-final-root binary/live/filesystem.squashfs
runtime_config=/work/vaultboot-final-root/etc/xdg/openbox/vaultboot-rc.xml
# Verify that the intended XML element is the only configuration content change.
python3 - "$runtime_config" "$new_config" <<'PY'
from pathlib import Path
import sys
old, new = (Path(p).read_text(encoding='utf-8') for p in sys.argv[1:])
rule = '    <application class="md.obsidian.Obsidian" type="normal"><decor>no</decor><maximized>yes</maximized><focus>yes</focus></application>\n'
if new.count(rule) != 1 or new.replace(rule, '', 1) != old:
    raise SystemExit('Refusing unexpected changes: expected only the exact new Obsidian window rule')
PY
install -m 0644 "$new_config" "$runtime_config"
touch -d "@$SOURCE_DATE_EPOCH" "$runtime_config"
install -m 0644 "$new_config" config/includes.chroot/etc/xdg/openbox/vaultboot-rc.xml
install -m 0644 "$new_config" chroot/etc/xdg/openbox/vaultboot-rc.xml
mksquashfs /work/vaultboot-final-root /work/vaultboot-final.squashfs \
    -noappend -no-progress -comp xz -b 1M -processors 2 -mem 1G
mv /work/vaultboot-final.squashfs binary/live/filesystem.squashfs
lb binary_checksums --force
# Trixie defaults to SHA256; preserve whichever checksum algorithms this build used.
checksum_maps=()
for checksum in binary/*sum.txt; do
    [[ -f $checksum ]] || continue
    checksum_maps+=(-map "/work/$checksum" "/${checksum##*/}")
done
(( ${#checksum_maps[@]} > 0 ))
# Replay the existing BIOS/EFI entries; only the rootfs and its checksum file change.
xorriso -indev "/previous/$prefix.iso" -outdev "/out/$prefix.iso" \
    -boot_image any replay \
    -map /work/binary/live/filesystem.squashfs /live/filesystem.squashfs \
    "${checksum_maps[@]}" -commit -end
python3 /src/scripts/make-usb-image.py --iso "/out/$prefix.iso" \
    --output "/out/$prefix.img" --persistence-mib 4096
gzip -n -1 "/out/$prefix.img"
cp "/previous/$prefix.packages.tsv" /out/
cp /previous/*-source.debian.tar.gz.part-* /out/
# The package sources are unchanged. Regenerate the exact live configuration archive.
lb source_live --force
lb source_checksums --force
tar cf /work/vaultboot-final-source.live.tar --sort=name source/live
touch -d "@$SOURCE_DATE_EPOCH" /work/vaultboot-final-source.live.tar
gzip -n -1 /work/vaultboot-final-source.live.tar
split --bytes=1700M --numeric-suffixes=0 --suffix-length=3 /work/vaultboot-final-source.live.tar.gz \
    "/out/$prefix-source.live.tar.gz.part-"
cp /previous/SOURCES.txt /previous/BUILD-INFO.txt /out/
cp /src/LICENSE /out/LICENSE.txt
printf 'Image configuration overlay commit: %s\nOverlay: exact md.obsidian.Obsidian Openbox window rule; rootfs and hybrid ISO repacked, source.live regenerated. Debian package source parts unchanged.\n' \
    "$OVERLAY_COMMIT" >> /out/BUILD-INFO.txt
printf 'Openbox configuration SHA256: %s\n' "$OVERLAY_XML_SHA256" >> /out/BUILD-INFO.txt
sha256sum /src/build/refinalize-amd64.sh | sed 's|/src/||' >> /out/BUILD-INFO.txt
cd /out
sha256sum -- *.iso *.img.gz *.layout.json *.packages.tsv *.part-* LICENSE.txt SOURCES.txt BUILD-INFO.txt > SHA256SUMS
sha256sum --check SHA256SUMS
echo 'Final AMD64 artifact set complete; boot and persistence validation still required.'
