#!/bin/bash
# Run inside the same isolated builder VM; preserve the prior image and sources.
set -Eeuo pipefail
umask 022
[[ $# == 3 ]] || { echo 'Usage: build/refinalize-session-pi.sh PREVIOUS_ARTIFACTS CLEAN_RAW_IMAGE NEW_OUTPUT' >&2; exit 2; }
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
[[ ${OVERLAY_COMMIT:-} =~ ^[0-9a-f]{40}$ ]]
[[ ${OVERLAY_SESSION_SHA256:-} =~ ^[0-9a-f]{64}$ ]]
[[ ${PREVIOUS_SESSION_SHA256:-} =~ ^[0-9a-f]{64}$ ]]
[[ ${PREVIOUS_RAW_SHA256:-} =~ ^[0-9a-f]{64}$ ]]
version=${VAULTBOOT_VERSION:-0.1.0-preview.1}
[[ $version =~ ^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$ ]]
prefix="vaultboot-${version}-pi3-arm64"
printf '%s  %s\n' "$PREVIOUS_RAW_SHA256" "$input" | sha256sum --check
(cd "$previous" && sha256sum --check --quiet SHA256SUMS)
new_session="$project/config/includes.chroot/usr/local/bin/vaultboot-session"
printf '%s  %s\n' "$OVERLAY_SESSION_SHA256" "$new_session" | sha256sum --check
mkdir -- "$output"
output=$(realpath -- "$output")
stage=$(mktemp -d /build/session-final.XXXXXXXX)
image="$stage/$prefix.img"
root="$stage/root"
mkdir "$root" "$stage/source"
cp --reflink=auto --sparse=always "$input" "$image"
loop=
cleanup() {
    status=$?
    trap - EXIT
    if mountpoint -q "$root"; then umount "$root" || true; fi
    if [[ -n $loop ]]; then losetup -d "$loop" || true; fi
    printf 'Pi session finalization workspace: %s (exit %s)\n' "$stage" "$status" >&2
    exit "$status"
}
trap cleanup EXIT
loop=$(losetup --find --show --partscan "$image")
udevadm settle
mount "${loop}p1" "$root"
printf '%s  %s\n' "$PREVIOUS_SESSION_SHA256" "$root/usr/local/bin/vaultboot-session" | sha256sum --check
install -m 0755 "$new_session" "$root/usr/local/bin/vaultboot-session"
sync
umount "$root"
LC_ALL=C e2fsck -fn "${loop}p1" > "$output/FILESYSTEM-CHECK.txt" 2>&1
cat "$output/FILESYSTEM-CHECK.txt"
! grep -Eq 'Warning|Fix\?|Clear\?' "$output/FILESYSTEM-CHECK.txt"
losetup -d "$loop"
loop=
python3 "$project/build/pi-layout.py" "$image" > "$output/$prefix.layout.json"
cp "$previous/$prefix.base-layout.json" "$previous/$prefix.packages.tsv" "$output/"
cp "$previous/"*-source.debian.tar.gz.part-* "$output/"
cp -r "$previous/boot-test" "$output/"
tar -xzf "$previous/$prefix-source.vaultboot.tar.gz" -C "$stage/source"
source_session="$stage/source/config/includes.chroot/usr/local/bin/vaultboot-session"
printf '%s  %s\n' "$PREVIOUS_SESSION_SHA256" "$source_session" | sha256sum --check
install -m 0755 "$new_session" "$source_session"
tar -C "$stage/source" --sort=name -cf "$stage/source.vaultboot.tar" LICENSE build config scripts tests
gzip -n -1 "$stage/source.vaultboot.tar"
cp "$stage/source.vaultboot.tar.gz" "$output/$prefix-source.vaultboot.tar.gz"
cp "$previous/LICENSE.txt" "$previous/SOURCES.txt" "$previous/BUILD-INFO.txt" "$output/"
raw_sha=$(sha256sum "$image" | cut -d' ' -f1)
printf 'Session overlay commit: %s\nPrevious session SHA256: %s\nFinal session SHA256: %s\nFinal raw image SHA256: %s\nOverlay: only vaultboot-session and matching project source file. Debian packages, firmware and package source parts unchanged.\n' \
    "$OVERLAY_COMMIT" "$PREVIOUS_SESSION_SHA256" "$OVERLAY_SESSION_SHA256" "$raw_sha" >> "$output/BUILD-INFO.txt"
sha256sum "$project/build/refinalize-session-pi.sh" | sed "s|$project/||" >> "$output/BUILD-INFO.txt"
gzip -n -1 -c "$image" > "$output/$prefix.img.gz"
cd "$output"
sha256sum -- *.img.gz *.json *.tsv *.part-* *.tar.gz boot-test/* LICENSE.txt BUILD-INFO.txt SOURCES.txt FILESYSTEM-CHECK.txt > SHA256SUMS
sha256sum --check --quiet SHA256SUMS
echo 'Session-corrected Pi artifacts complete; final-byte ARM boot verification required.'
