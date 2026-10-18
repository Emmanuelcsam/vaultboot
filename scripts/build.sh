#!/bin/bash
# Build a complete artifact set without touching host disks or installing host packages.
set -Eeuo pipefail
umask 022
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
output=${1:-"$root/dist"}
[[ $# -le 1 ]] || { echo 'Usage: scripts/build.sh [NEW_OUTPUT_DIRECTORY]' >&2; exit 2; }
command -v docker >/dev/null || { echo 'Docker is required.' >&2; exit 1; }
[[ $(uname -m) == x86_64 ]] || { echo 'Build on an amd64/x86_64 Linux host.' >&2; exit 1; }
[[ ! -e $output && ! -L $output ]] || { echo "Refusing existing output path: $output" >&2; exit 1; }
mkdir -- "$output"
output=$(cd -- "$output" && pwd)
version=${VAULTBOOT_VERSION:-dev}
[[ $version =~ ^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$ ]] || { echo 'Invalid VAULTBOOT_VERSION.' >&2; exit 1; }
cpus=${BUILD_CPUS:-2}
[[ $cpus =~ ^[1-8]$ ]] || { echo 'BUILD_CPUS must be an integer from 1 to 8.' >&2; exit 1; }
memory=${BUILD_MEMORY:-6g}
[[ $memory =~ ^[1-9][0-9]*[gm]$ ]] || { echo 'BUILD_MEMORY must be a Docker memory size, e.g. 6g.' >&2; exit 1; }
commit=$(git -C "$root" rev-parse HEAD 2>/dev/null || printf 'uncommitted')
epoch=${SOURCE_DATE_EPOCH:-$(git -C "$root" log -1 --format=%ct 2>/dev/null || date +%s)}
[[ $epoch =~ ^[0-9]+$ ]] || { echo 'Invalid SOURCE_DATE_EPOCH.' >&2; exit 1; }
image=${BUILDER_IMAGE:-vaultboot-builder:trixie}
docker build --pull -t "$image" -f "$root/build/Dockerfile" "$root/build"
# Privilege is needed for debootstrap/chroot mounts. It grants host-device access:
# run only trusted project code on a disposable build machine. --rm removes the
# private work volume; scripts operate on image files and do not format host disks.
docker run --rm --privileged --cpus "$cpus" --memory "$memory" --memory-swap "$memory" \
    --mount "type=bind,src=$root,dst=/src,readonly" \
    --mount "type=bind,src=$output,dst=/out" \
    --mount type=volume,destination=/work \
    -e "VAULTBOOT_VERSION=$version" -e "BUILD_CPUS=$cpus" -e "SOURCE_DATE_EPOCH=$epoch" \
    -e "BUILD_COMMIT=$commit" -e "OUTPUT_UID=$(id -u)" -e "OUTPUT_GID=$(id -g)" \
    "$image" 2>&1 | tee "$output/build.log"
