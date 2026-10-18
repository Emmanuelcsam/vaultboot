#!/bin/bash
# Run inside a disposable amd64 Linux VM, with ARM binfmt enabled inside that VM.
# The only writable disk target is a new regular image file under /build.
set -Eeuo pipefail
umask 022
[[ $# == 2 ]] || { echo 'Usage: build/pi.sh DEBIAN_PI_TAR_XZ NEW_OUTPUT_DIRECTORY' >&2; exit 2; }
[[ $(id -u) == 0 ]] || { echo 'Run as root inside the isolated builder VM.' >&2; exit 1; }
systemd-detect-virt --vm --quiet || { echo 'A disposable builder VM is required.' >&2; exit 1; }
[[ -d /build ]] || { echo 'Mount the disposable work disk at /build first.' >&2; exit 1; }
# Service mount namespaces must never inherit the chroot mounts and keep the
# filesystem active after this process unmounts it.
if [[ ${VAULTBOOT_MOUNT_NAMESPACE:-} != "$(readlink /proc/self/ns/mnt)" ]]; then
    exec unshare --mount --propagation private -- bash -c \
        'export VAULTBOOT_MOUNT_NAMESPACE=$(readlink /proc/self/ns/mnt); exec "$@"' \
        vaultboot-private-mounts "$0" "$@"
fi
ulimit -n 65536
project=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
base=$1
output=$2
[[ -f $base && ! -L $base ]] || { echo 'Base must be a regular archive, not a device or symlink.' >&2; exit 1; }
[[ ! -e $output && ! -L $output ]] || { echo 'Output path already exists.' >&2; exit 1; }
base=$(realpath -- "$base")
mkdir -- "$output"
output=$(realpath -- "$output")
version=${VAULTBOOT_VERSION:-dev}
[[ $version =~ ^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$ ]] || exit 2
prefix="vaultboot-${version}-pi3-arm64"
work=$(mktemp -d /build/vaultboot-pi.XXXXXXXX)
root="$work/root"
image="$work/$prefix.img"
loop=
mkdir "$root"
cleanup() {
    status=$?
    trap - EXIT
    for target in "$root/run" "$root/sys" "$root/proc" "$root/dev" "$root/boot/firmware" "$root"; do
        if mountpoint -q "$target"; then umount -R "$target" || true; fi
    done
    if [[ -n $loop ]]; then losetup -d "$loop" || true; fi
    printf 'Pi build workspace: %s (exit %s)\n' "$work" "$status" >&2
    exit "$status"
}
trap cleanup EXIT
expected=8b0badd4585ece8f792bae913325c2d28ace32d0df0b97a5d27e90629d1dd98f4bd7cf6531a3bd3a9b24fbb62c761b06268497d48766553b071019ec357a27ec
printf '%s  %s\n' "$expected" "$base" | sha512sum --check --status
python3 - "$base" "$image" <<'PY'
from pathlib import Path
import shutil, sys, tarfile
archive, output = sys.argv[1:]
found = False
with tarfile.open(archive, mode='r|xz') as tar:
    for member in tar:
        if not member.isfile() or not member.name.endswith('.raw') or found:
            raise SystemExit('Expected exactly one regular .raw member in the official Debian archive')
        with tar.extractfile(member) as source, open(output, 'xb') as target:
            shutil.copyfileobj(source, target, 8 * 1024 * 1024)
        found = True
if not found:
    raise SystemExit('The archive did not contain a disk image')
PY
python3 "$project/build/pi-layout.py" "$image" > "$output/$prefix.base-layout.json"
read -r boot_partition root_partition < <(python3 - "$output/$prefix.base-layout.json" <<'PY'
import json, sys
data = json.load(open(sys.argv[1]))
print(data['boot_partition'], data['root_partition'])
PY
)
[[ $(stat -c %s "$image") -lt 8589934592 ]] || { echo 'Unexpected base image size.' >&2; exit 1; }
truncate -s 8G "$image"
# The validated layout has one firmware partition followed by Linux root.
printf ',+\n' | sfdisk --no-reread -N "$root_partition" "$image"
python3 "$project/build/pi-layout.py" "$image" > "$work/resized-layout.json"
loop=$(losetup --find --show --partscan "$image")
udevadm settle
boot_device="${loop}p${boot_partition}"
root_device="${loop}p${root_partition}"
[[ $(blkid -s TYPE -o value "$boot_device") == vfat ]]
[[ $(blkid -s TYPE -o value "$root_device") == ext4 ]]
e2fsck -pf "$root_device" || [[ $? == 1 ]]
resize2fs "$root_device"
mount "$root_device" "$root"
mkdir -p "$root/boot/firmware"
mount "$boot_device" "$root/boot/firmware"
for target in dev proc sys; do
    mount --rbind "/$target" "$root/$target"
    mount --make-rslave "$root/$target"
done
mount -t tmpfs -o mode=755 tmpfs "$root/run"
[[ $(chroot "$root" dpkg --print-architecture) == arm64 ]]
cp --dereference /etc/resolv.conf "$work/resolv.conf"
rm -f "$root/etc/resolv.conf"
cp "$work/resolv.conf" "$root/etc/resolv.conf"
cat > "$root/usr/sbin/policy-rc.d" <<'EOF'
#!/bin/sh
exit 101
EOF
chmod 755 "$root/usr/sbin/policy-rc.d"
cat > "$root/etc/apt/apt.conf.d/90vaultboot" <<'EOF'
APT::Install-Recommends "false";
APT::Install-Suggests "false";
Acquire::Retries "3";
EOF
chroot "$root" env DEBIAN_FRONTEND=noninteractive apt-get update
# Reuse the appliance packages, omitting the amd64 kernel, PC firmware and live overlay.
mapfile -t packages < <(sed '/^[[:space:]]*#/d; /^[[:space:]]*$/d' "$project/config/package-lists/vaultboot.list.chroot" |
    grep -Ev '^(linux-image-amd64|live-.*|user-setup|intel-microcode|amd64-microcode|grub-.*|grub2-common|fdisk|firmware-(amd-graphics|intel-graphics|iwlwifi|atheros|realtek))$')
chroot "$root" env DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "${packages[@]}"
# A preconfigured local appliance does not need provisioning or a remote shell.
mapfile -t unwanted < <(chroot "$root" dpkg-query -W '-f=${db:Status-Abbrev} ${binary:Package}\n' |
    awk '$1 == "ii" && $2 ~ /^(openssh-server|openssh-client|cloud-init|cloud-guest-utils|cloud-initramfs-.*|netplan.io|netplan-generator|python3-netplan)$/ {print $2}')
if (( ${#unwanted[@]} )); then
    chroot "$root" env DEBIAN_FRONTEND=noninteractive apt-get purge -y "${unwanted[@]}"
fi
cp -r --preserve=mode,timestamps "$project/config/includes.chroot/." "$root/"
find "$root/usr/local/lib/vaultboot" -type d -name __pycache__ -prune -exec rm -rf -- {} +
cp "$project/config/hooks/live/0100-vaultboot.hook.chroot" "$root/tmp/vaultboot-configure"
chroot "$root" sh /tmp/vaultboot-configure
rm "$root/tmp/vaultboot-configure"
chroot "$root" id writer >/dev/null 2>&1 || chroot "$root" useradd -m -s /bin/bash writer
for group in audio video render plugdev netdev sudo; do
    if chroot "$root" getent group "$group" >/dev/null; then chroot "$root" usermod -aG "$group" writer; fi
done
chroot "$root" passwd --lock root
printf 'writer:live\n' | chroot "$root" chpasswd
printf 'writer ALL=(ALL:ALL) NOPASSWD: ALL\n' > "$root/etc/sudoers.d/90-vaultboot"
chmod 440 "$root/etc/sudoers.d/90-vaultboot"
chroot "$root" visudo -cf /etc/sudoers.d/90-vaultboot
printf 'vaultboot\n' > "$root/etc/hostname"
printf '127.0.0.1 localhost\n127.0.1.1 vaultboot\n::1 localhost ip6-localhost ip6-loopback\n' > "$root/etc/hosts"
printf 'en_US.UTF-8 UTF-8\n' > "$root/etc/locale.gen"
chroot "$root" locale-gen
chroot "$root" update-locale LANG=en_US.UTF-8
printf 'auto lo\niface lo inet loopback\n' > "$root/etc/network/interfaces"
rm -f "$root/etc/netplan/90-default.yaml"
systemctl --root "$root" disable systemd-networkd.service systemd-networkd.socket systemd-networkd-wait-online.service || true
systemctl --root "$root" set-default graphical.target
# These are ordinary writable filesystems, with no live overlay or live-config.
test -s "$root/boot/firmware/config.txt"
test -s "$root/boot/firmware/cmdline.txt"
test -s "$root/etc/fstab"
chroot "$root" dpkg-query -W linux-image-arm64 raspi-firmware
mkdir "$output/boot-test"
kernel=$(find "$root/boot" -maxdepth 1 -type f -name 'vmlinuz-*' | sort -V | tail -1)
test -n "$kernel"
kernel_version=${kernel##*/vmlinuz-}
grep -Eq '^CONFIG_DRM_VC4=(y|m)$' "$root/boot/config-$kernel_version"
grep -Eq '^CONFIG_DRM_VIRTIO_GPU=(y|m)$' "$root/boot/config-$kernel_version"
cp "$kernel" "$output/boot-test/vmlinuz"
cp "$root/boot/initrd.img-$kernel_version" "$output/boot-test/initrd.img"
cp "$root/boot/config-$kernel_version" "$output/boot-test/config"
chroot "$root" dpkg-query -W '-f=${binary:Package}\t${Version}\t${source:Package}\t${source:Version}\n' |
    LC_ALL=C sort > "$output/$prefix.packages.tsv"
python3 "$project/build/pi-sources.py" --chroot "$root" --output "$work/sources"
tar -C "$work/sources" -cf "$work/$prefix-source.debian.tar" --sort=name debian source-selection.txt
gzip -n -1 "$work/$prefix-source.debian.tar"
split --bytes=1700M --numeric-suffixes=0 --suffix-length=3 "$work/$prefix-source.debian.tar.gz" \
    "$output/$prefix-source.debian.tar.gz.part-"
# Keep project code with the image; no local git credentials or histories enter this tar.
tar -C "$project" -cf "$work/$prefix-source.vaultboot.tar" --sort=name \
    --exclude='__pycache__' --exclude='*.pyc' LICENSE build config scripts tests
gzip -n -1 "$work/$prefix-source.vaultboot.tar"
cp "$work/$prefix-source.vaultboot.tar.gz" "$output/"
chroot "$root" apt-get clean
rm -f "$root/usr/sbin/policy-rc.d" "$root/etc/machine-id" "$root/var/lib/dbus/machine-id"
touch "$root/etc/machine-id"
rm -f "$root/etc/ssh/ssh_host_"* "$root/root/.bash_history" "$root/var/lib/systemd/random-seed"
rm -rf "$root/root/.ssh" "$root/var/lib/cloud" "$root/var/lib/apt/lists/"*
rm -f "$root/etc/resolv.conf"
ln -s /run/NetworkManager/resolv.conf "$root/etc/resolv.conf"
sync
for target in run sys proc dev boot/firmware; do umount -R "$root/$target"; done
umount "$root"
if ! LC_ALL=C e2fsck -fn "$root_device" > "$work/fsck-final.txt" 2>&1; then
    cat "$work/fsck-final.txt" >&2
    exit 1
fi
cat "$work/fsck-final.txt"
# e2fsck can return zero when -n skips recovery of a mounted/dirty journal.
if grep -Eq 'Warning|Fix\?|Clear\?' "$work/fsck-final.txt"; then
    echo 'The image filesystem must be clean and fully unmounted before packaging.' >&2
    exit 1
fi
losetup -d "$loop"
loop=
# The finished root filesystem keeps its full 8 GiB-image extent. The new raw
# slot is appended outside it, with no filesystem mounting or repartitioning.
python3 "$project/build/preload-layout.py" append-pi "$image" > "$work/offline-layout.json"
python3 "$project/build/pi-layout.py" "$image" > "$output/$prefix.layout.json"
gzip -n -1 "$image"
cp "$image.gz" "$output/"
cp "$project/LICENSE" "$output/LICENSE.txt"
printf 'Vaultboot version: %s\nArchitecture: arm64\nTarget: Raspberry Pi 3 (hardware validation required)\nBase: Debian trixie raspi 20261017-2634\nBase SHA512: %s\nBuild commit: %s\n' \
    "$version" "$expected" "$(git -C "$project" rev-parse HEAD 2>/dev/null || printf unknown)" > "$output/BUILD-INFO.txt"
cat > "$output/SOURCES.txt" <<'EOF'
These are the exact Debian sources for all installed packages, including Built-Using
and Static-Built-Using code. Join the numbered source.debian.tar.gz.part-* files in
lexical order and extract the resulting gzip tar archive. The source.vaultboot.tar.gz
contains the image build scripts and runtime. Obsidian is downloaded separately from
its publisher on first run and is not included in this image or these archives.
EOF
cd "$output"
sha256sum -- *.img.gz *.json *.tsv *.part-* *.tar.gz boot-test/* LICENSE.txt BUILD-INFO.txt SOURCES.txt > SHA256SUMS
echo 'Pi image and corresponding sources complete. Boot on real target hardware before claiming hardware support.'
