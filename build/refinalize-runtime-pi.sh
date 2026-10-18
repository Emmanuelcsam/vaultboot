#!/bin/bash
# Correct only reviewed Python runtime modules in a NEW Pi image copy.
set -Eeuo pipefail
umask 022
[[ $# == 3 ]] || { echo 'Usage: build/refinalize-runtime-pi.sh PREVIOUS_ARTIFACTS CLEAN_RAW_IMAGE NEW_OUTPUT' >&2; exit 2; }
[[ $(id -u) == 0 ]] && systemd-detect-virt --vm --quiet
if [[ ${VAULTBOOT_MOUNT_NAMESPACE:-} != "$(readlink /proc/self/ns/mnt)" ]]; then
    exec unshare --mount --propagation private -- bash -c \
        'export VAULTBOOT_MOUNT_NAMESPACE=$(readlink /proc/self/ns/mnt); exec "$@"' \
        vaultboot-private-mounts "$0" "$@"
fi
ulimit -n 65536
project=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
previous=$(realpath -- "$1")
input=$2
output=$3
[[ -f $input && ! -L $input && $(stat -c %s "$input") == 8589934592 ]]
[[ ! -e $output && ! -L $output ]]
[[ ${BASE_COMMIT:-} =~ ^[0-9a-f]{40}$ ]]
[[ ${OVERLAY_COMMIT:-} =~ ^[0-9a-f]{40}$ ]]
[[ ${PREVIOUS_RAW_SHA256:-} =~ ^[0-9a-f]{64}$ ]]
version=${VAULTBOOT_VERSION:?Set the exact image version}
[[ $version =~ ^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$ ]]
prefix="vaultboot-${version}-pi3-arm64"
grep -Fxq "Vaultboot version: $version" "$previous/BUILD-INFO.txt"
grep -Fxq "Build commit: $BASE_COMMIT" "$previous/BUILD-INFO.txt"
printf '%s  %s\n' "$PREVIOUS_RAW_SHA256" "$input" | sha256sum --check
(cd "$previous" && sha256sum --check --quiet SHA256SUMS)
[[ $(git -C "$project" rev-parse HEAD) == "$OVERLAY_COMMIT" ]]
mkdir -- "$output"
output=$(realpath -- "$output")
stage=$(mktemp -d /build/runtime-final.XXXXXXXX)
image="$stage/$prefix.img"
root="$stage/root"
mkdir "$root"
cp --reflink=auto --sparse=always "$input" "$image"
loop=
cleanup() {
    status=$?
    trap - EXIT
    if mountpoint -q "$root"; then umount "$root" || true; fi
    if [[ -n $loop ]]; then losetup -d "$loop" || true; fi
    printf 'Pi runtime finalization workspace: %s (exit %s)\n' "$stage" "$status" >&2
    exit "$status"
}
trap cleanup EXIT
loop=$(losetup --find --show --partscan "$image")
udevadm settle
mount "${loop}p1" "$root"
chroot "$root" dpkg-query -W '-f=${binary:Package}\t${Version}\t${source:Package}\t${source:Version}\n' |
    LC_ALL=C sort > "$stage/packages-before.tsv"
cmp "$previous/$prefix.packages.tsv" "$stage/packages-before.tsv"
python3 "$project/build/runtime-overlay.py" --root "$root" --base-commit "$BASE_COMMIT" \
    --final-commit "$OVERLAY_COMMIT" --manifest "$output/RUNTIME-OVERLAY.json"
chroot "$root" dpkg-query -W '-f=${binary:Package}\t${Version}\t${source:Package}\t${source:Version}\n' |
    LC_ALL=C sort > "$stage/packages-after.tsv"
cmp "$stage/packages-before.tsv" "$stage/packages-after.tsv"
sync
umount "$root"
LC_ALL=C e2fsck -fn "${loop}p1" > "$output/FILESYSTEM-CHECK.txt" 2>&1
cat "$output/FILESYSTEM-CHECK.txt"
! grep -Eq 'Warning|Fix\?|Clear\?' "$output/FILESYSTEM-CHECK.txt"
losetup -d "$loop"
loop=
# The entire firmware partition and boot records were outside the mounted root.
cmp --bytes=512 "$input" "$image"
cmp --bytes=$((1046528 * 512)) --ignore-initial=$((2048 * 512)) "$input" "$image"
python3 "$project/build/pi-layout.py" "$image" > "$output/$prefix.layout.json"
python3 - "$previous/$prefix.layout.json" "$output/$prefix.layout.json" <<'PY'
import json,sys
old,new=(json.load(open(p)) for p in sys.argv[1:])
# sfdisk embeds its input pathname. Every actual partition field must match.
for value in (old,new):
    table=value.get('partitiontable',value)
    table.pop('device',None)
    for part in table.get('partitions',[]):part.pop('node',None)
assert old == new, 'Pi partition/firmware layout changed'
PY
cp "$previous/$prefix.base-layout.json" "$previous/$prefix.packages.tsv" "$output/"
cp "$previous/$prefix"-source.debian.tar.gz.part-* "$output/"
cp -r "$previous/boot-test" "$output/"
git -C "$project" archive --format=tar "$OVERLAY_COMMIT" | gzip -n -1 > "$output/$prefix-source.vaultboot.tar.gz"
cp "$previous/LICENSE.txt" "$previous/SOURCES.txt" "$output/"
# Retain original build provenance and distinguish the final runtime source.
sed '/^Project source archive commit:/d; /^The source.vaultboot archive preserves/d' "$previous/BUILD-INFO.txt" > "$output/BUILD-INFO.txt"
raw_sha=$(sha256sum "$image" | cut -d' ' -f1)
printf 'Runtime overlay commit: %s\nProject source archive commit: %s\nPrevious raw image SHA256: %s\nFinal raw image SHA256: %s\nOverlay: only the files in RUNTIME-OVERLAY.json changed. Installed package manifest, firmware, kernel/initramfs and corresponding Debian source archives are unchanged.\nThe final project archive is a complete git archive, including the exact runtime overlay and the refinalization helper. The only allowed package-list/build-script adjustment adds fdisk for the PC installer and excludes it from Pi; effective Pi package selection is verified unchanged.\n' \
    "$OVERLAY_COMMIT" "$OVERLAY_COMMIT" "$PREVIOUS_RAW_SHA256" "$raw_sha" >> "$output/BUILD-INFO.txt"
sha256sum "$project/build/refinalize-runtime-pi.sh" "$project/build/runtime-overlay.py" | sed "s|$project/||" >> "$output/BUILD-INFO.txt"
gzip -n -1 -c "$image" > "$output/$prefix.img.gz"
cd "$output"
sha256sum -- *.img.gz *.json *.tsv *.part-* *.tar.gz boot-test/* LICENSE.txt BUILD-INFO.txt SOURCES.txt FILESYSTEM-CHECK.txt > SHA256SUMS
sha256sum --check --quiet SHA256SUMS
chmod 444 "$image"
echo 'Runtime-corrected Pi artifacts complete; final-byte ARM boot verification required.'
