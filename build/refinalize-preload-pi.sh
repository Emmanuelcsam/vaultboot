#!/bin/bash
# Add offline-preload support to a NEW copy of the verified preview.2 Pi image.
set -Eeuo pipefail
umask 022
[[ $# == 3 ]] || { echo 'Usage: build/refinalize-preload-pi.sh PREVIOUS_ARTIFACTS CLEAN_RAW_IMAGE NEW_OUTPUT' >&2; exit 2; }
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
version=${VAULTBOOT_VERSION:?Set the new image version}
previous_version=${PREVIOUS_VERSION:?Set the previous image version}
[[ $version =~ ^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$ && $previous_version =~ ^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$ ]]
[[ $version != "$previous_version" ]]
prefix="vaultboot-${version}-pi3-arm64"
old_prefix="vaultboot-${previous_version}-pi3-arm64"
grep -Fxq "Vaultboot version: $previous_version" "$previous/BUILD-INFO.txt"
grep -Fxq "Project source archive commit: $BASE_COMMIT" "$previous/BUILD-INFO.txt"
printf '%s  %s\n' "$PREVIOUS_RAW_SHA256" "$input" | sha256sum --check
(cd "$previous" && sha256sum --check --quiet SHA256SUMS)
[[ $(git -C "$project" rev-parse HEAD) == "$OVERLAY_COMMIT" ]]
mkdir -- "$output"
output=$(realpath -- "$output")
stage=$(mktemp -d /build/preload-final.XXXXXXXX)
image="$stage/$prefix.img"
root="$stage/root"
mkdir "$root"
cp --reflink=auto --sparse=always "$input" "$image"
chmod 644 "$image"
loop=
cleanup() {
    status=$?
    trap - EXIT
    if mountpoint -q "$root"; then umount "$root" || true; fi
    if [[ -n $loop ]]; then losetup -d "$loop" || true; fi
    printf 'Pi preload finalization workspace: %s (exit %s)\n' "$stage" "$status" >&2
    exit "$status"
}
trap cleanup EXIT
python3 "$project/build/preload-layout.py" append-pi "$image" > "$stage/offline-layout.json"
loop=$(losetup --find --show --partscan "$image")
udevadm settle
LC_ALL=C e2fsck -fn "${loop}p1" > "$stage/fsck-before.txt" 2>&1
! grep -Eq 'Warning|Fix\?|Clear\?' "$stage/fsck-before.txt"
mount "${loop}p1" "$root"
chroot "$root" dpkg-query -W '-f=${binary:Package}\t${Version}\t${source:Package}\t${source:Version}\n' |
    LC_ALL=C sort > "$stage/packages-before.tsv"
cmp "$previous/$old_prefix.packages.tsv" "$stage/packages-before.tsv"
python3 "$project/build/preload-overlay.py" --root "$root" --base-commit "$BASE_COMMIT" \
    --final-commit "$OVERLAY_COMMIT" --manifest "$output/PRELOAD-OVERLAY.json"
# Enabling is a declarative symlink in the overlay; no service runs in the builder.
test "$(readlink "$root/etc/systemd/system/graphical.target.wants/vaultboot-preload.service")" = ../vaultboot-preload.service
test ! -e "$root/var/lib/vaultboot/offline"
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
# Pi ROM-visible hybrid MBR and the entire firmware partition stay byte-identical.
cmp --bytes=512 "$input" "$image"
cmp --bytes=$((1046528 * 512)) --ignore-initial=$((2048 * 512)) "$input" "$image"
python3 "$project/build/pi-layout.py" "$image" > "$output/$prefix.layout.json"
python3 "$project/build/preload-layout.py" inspect "$image" > "$stage/offline-final.json"
cmp "$stage/offline-layout.json" "$stage/offline-final.json"
python3 - "$previous/$old_prefix.layout.json" "$output/$prefix.layout.json" <<'PY'
import json,sys
old,new=(json.load(open(path))['partitiontable'] for path in sys.argv[1:])
old_parts={p['uuid']:p for p in old['partitions']}
new_parts={p['uuid']:p for p in new['partitions']}
assert set(old_parts) < set(new_parts) and len(new_parts)==len(old_parts)+1
for identity,part in old_parts.items():
    a,b=dict(part),dict(new_parts[identity])
    a.pop('node',None); b.pop('node',None)
    assert a==b, 'An original Pi partition changed'
for field in ('label','id','unit','firstlba','sectorsize'):
    assert old.get(field)==new.get(field), 'An original disk identity/geometry field changed'
PY
cp "$previous/$old_prefix.base-layout.json" "$output/$prefix.base-layout.json"
cp "$previous/$old_prefix.packages.tsv" "$output/$prefix.packages.tsv"
for source in "$previous/$old_prefix"-source.debian.tar.gz.part-*; do
    suffix=${source##*.part-}
    [[ $suffix =~ ^[0-9]{3}$ ]]
    cp "$source" "$output/$prefix-source.debian.tar.gz.part-$suffix"
    cmp "$source" "$output/$prefix-source.debian.tar.gz.part-$suffix"
done
cp -r "$previous/boot-test" "$output/"
git -C "$project" archive --format=tar "$OVERLAY_COMMIT" | gzip -n -1 > "$output/$prefix-source.vaultboot.tar.gz"
cp "$previous/LICENSE.txt" "$output/"
cp "$previous/BUILD-INFO.txt" "$output/BASE-BUILD-INFO.txt"
raw_sha=$(sha256sum "$image" | cut -d' ' -f1)
cat > "$output/BUILD-INFO.txt" <<EOF
Vaultboot version: $version
Architecture: arm64
Target: Raspberry Pi 3 (hardware validation required)
Base project commit: $BASE_COMMIT
Preload overlay commit: $OVERLAY_COMMIT
Project source archive commit: $OVERLAY_COMMIT
Previous raw image SHA256: $PREVIOUS_RAW_SHA256
Final raw image SHA256: $raw_sha
Base package-build and previous runtime provenance: BASE-BUILD-INFO.txt
Overlay: exactly the files and service enablement in PRELOAD-OVERLAY.json; an empty 256 MiB raw offline partition is appended and GPT metadata updated. The entire hybrid MBR, firmware partition, original partition identities/extents, installed package manifest, kernel/initramfs and corresponding Debian source bytes are unchanged. No Obsidian package is included. Clean filesystem checks are in FILESYSTEM-CHECK.txt.
EOF
sha256sum "$project/build/refinalize-preload-pi.sh" "$project/build/preload-overlay.py" "$project/build/preload-layout.py" |
    sed "s|$project/||" >> "$output/BUILD-INFO.txt"
cat > "$output/SOURCES.txt" <<EOF
The numbered $prefix-source.debian.tar.gz.part-* files contain the exact Debian
sources for installed packages and their Built-Using/Static-Built-Using code.
Their bytes are unchanged from the verified $previous_version Pi source collection.
Join only this architecture's parts in lexical order:
  cat $prefix-source.debian.tar.gz.part-* > $prefix-source.debian.tar.gz
  tar -xzf $prefix-source.debian.tar.gz
The source.vaultboot.tar.gz is the complete project at $OVERLAY_COMMIT, including
the new runtime, empty-slot builder and this reproducible refinalization helper.
BASE-BUILD-INFO.txt records the original image/package-build provenance.
Obsidian is fetched separately from its publisher and is absent from these files.
EOF
gzip -n -1 -c "$image" > "$output/$prefix.img.gz"
cd "$output"
sha256sum -- *.img.gz *.json *.tsv *.part-* *.tar.gz boot-test/* LICENSE.txt BUILD-INFO.txt BASE-BUILD-INFO.txt SOURCES.txt FILESYSTEM-CHECK.txt > SHA256SUMS
sha256sum --check --quiet SHA256SUMS
chmod 444 "$image"
echo 'Offline-preload Pi artifacts complete; final-byte ARM boot verification required.'
