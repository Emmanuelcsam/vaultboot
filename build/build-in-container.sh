#!/bin/bash
# Runs only in the privileged, disposable Debian builder container.
set -Eeuo pipefail
umask 022

[[ $(id -u) == 0 ]] || { echo 'Builder must run as root inside the container.' >&2; exit 1; }
[[ -d /src/config && -d /out ]] || { echo 'Missing /src or /out bind mount.' >&2; exit 1; }
[[ -z $(find /work -mindepth 1 -maxdepth 1 -print -quit) ]] || {
    echo '/work must be empty; use a fresh builder volume.' >&2; exit 1;
}
version=${VAULTBOOT_VERSION:-dev}
[[ $version =~ ^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$ ]] || { echo 'Invalid version.' >&2; exit 1; }
cpus=${BUILD_CPUS:-2}
[[ $cpus =~ ^[1-8]$ ]] || { echo 'BUILD_CPUS must be an integer from 1 to 8.' >&2; exit 1; }
export SOURCE_DATE_EPOCH=${SOURCE_DATE_EPOCH:-$(date +%s)}
[[ $SOURCE_DATE_EPOCH =~ ^[0-9]+$ ]] || exit 1
trap 'chown -R "${OUTPUT_UID:-0}:${OUTPUT_GID:-0}" /out' EXIT
cd /work

lb config --mode debian --distribution trixie --architectures amd64 \
    --binary-image iso-hybrid --image-name vaultboot \
    --archive-areas 'main contrib non-free-firmware' \
    --debootstrap-options '--variant=minbase' \
    --apt-recommends false --apt-secure true --apt-source-archives true \
    --bootloaders 'syslinux grub-efi' --uefi-secure-boot disable \
    --debian-installer false --firmware-binary false --firmware-chroot false \
    --memtest none --linux-flavours amd64 \
    --bootappend-live 'boot=live components username=writer hostname=vaultboot persistence persistence-label=vaultboot-data locales=en_US.UTF-8 quiet console=tty0 console=ttyS0,115200' \
    --iso-application 'Vaultboot Debian Obsidian appliance' \
    --iso-publisher 'Vaultboot community project' --iso-volume VAULTBOOT \
    --mksquashfs-options "-comp xz -b 1M -processors $cpus -mem 1G" \
    --source true --source-images tar --compression gzip
cp -a /src/config/. config/
# Local tests may import included Python modules. Never bake those caches into the OS.
find config -type d -name __pycache__ -prune -exec rm -rf -- {} +
find config -type f \( -name '*.pyc' -o -name '*.pyo' \) -delete
lb build

# live-build warns and continues on missing source downloads. Distribution must fail.
if [[ -e source/missing-source.txt ]]; then
    cat source/missing-source.txt >&2
    echo 'Refusing release: corresponding Debian source downloads are incomplete.' >&2
    exit 1
fi
iso=$(find . -maxdepth 1 -type f -name '*.hybrid.iso' -print -quit)
[[ -n $iso && -s $iso ]] || { echo 'No hybrid ISO produced.' >&2; exit 1; }
prefix="vaultboot-${version}-amd64"
cp -- "$iso" "/out/${prefix}.iso"
chroot chroot dpkg-query -W '-f=${binary:Package}\t${Version}\t${source:Package}\t${source:Version}\n' \
    | LC_ALL=C sort > "/out/${prefix}.packages.tsv"
python3 /src/scripts/make-usb-image.py --iso "/out/${prefix}.iso" \
    --output "/out/${prefix}.img" --persistence-mib 4096
gzip -n -1 "/out/${prefix}.img"

shopt -s nullglob
sources=(vaultboot-source.debian.tar* vaultboot-source.live.tar*)
[[ ${#sources[@]} == 2 ]] || { echo 'Expected Debian and live configuration source archives.' >&2; exit 1; }
for archive in "${sources[@]}"; do
    # GitHub Release has a 2 GiB per-asset limit. Always split so instructions are uniform.
    split --bytes=1700M --numeric-suffixes=0 --suffix-length=3 \
        "$archive" "/out/${prefix}-${archive#vaultboot-}.part-"
done
cp /src/build/SOURCES.txt /out/SOURCES.txt
printf 'Vaultboot version: %s\nDebian suite: trixie\nArchitecture: amd64\nSOURCE_DATE_EPOCH: %s\nBuild commit: %s\n' \
    "$version" "$SOURCE_DATE_EPOCH" "${BUILD_COMMIT:-unknown}" > /out/BUILD-INFO.txt
dpkg-query -W live-build debootstrap xorriso >> /out/BUILD-INFO.txt
cd /out
sha256sum -- *.iso *.img.gz *.layout.json *.packages.tsv *.part-* SOURCES.txt BUILD-INFO.txt > SHA256SUMS
echo 'Build complete. Boot validation is required before publishing a non-draft release.'
