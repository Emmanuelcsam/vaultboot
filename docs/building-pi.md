# Build the Raspberry Pi image

The Pi image uses the official Debian 13 ARM64 Raspberry Pi disk image as its
base. It preserves Debian's Pi firmware, kernel, device trees, and partition
identifiers, then adds the same minimal Vaultboot session used on PCs.

Run the builder **inside a disposable Debian 13 amd64 virtual machine** with
at least 4 GiB RAM and 64 GiB free disk space. ARM programs run through QEMU's
user-mode emulator inside that VM. The builder refuses to run outside a VM;
do not register ARM interpreters or format disks on a shared host to build this.

Install these tools in the disposable VM:

```sh
sudo apt-get update
sudo apt-get install -y qemu-user-static binfmt-support debootstrap parted \
  fdisk dosfstools e2fsprogs python3 curl xz-utils ca-certificates rsync git
sudo systemctl restart systemd-binfmt
sudo mkdir -p /build
```

Use a fresh checkout of the release tag and download the pinned base:

```sh
curl --fail --location --retry 3 -o /build/debian-pi-base.tar.xz \
  https://cloud.debian.org/images/cloud/trixie/20261017-2634/debian-13-raspi-arm64-20261017-2634.tar.xz
```

`build/pi.sh` verifies that archive against the official SHA-512 recorded in
the script before extracting or changing it. It only creates and resizes a
new regular image file. Loop devices and ARM interpreter registration remain
inside the disposable VM.

From the checkout, choose a new output directory:

```sh
sudo env VAULTBOOT_VERSION=0.1.0-preview.3 \
  ./build/pi.sh /build/debian-pi-base.tar.xz /build/pi-artifacts
```

The result is an 8 GiB system image plus a 256 MiB offline-preparation partition,
compressed as `.img.gz`, intended for a
**16 GB or larger microSD card**. It uses an ordinary writable root filesystem,
so notes persist without a separate live-overlay partition. Larger cards do
not automatically enlarge the root filesystem.

The output also includes the installed package manifest, matching Debian
source archives (including incorporated sources), project source archive,
checksums, and kernel/initramfs files for software-emulation checks. Verify
the complete `SHA256SUMS` before distributing the artifacts. Never publish
an image without its corresponding source archives.

`tests/vm/arm-console.py` can test the image's ARM64 software with QEMU's
generic `virt` machine and the exported kernel/initramfs. This does not test
the Raspberry Pi firmware boot chain or physical graphics, Wi-Fi, or storage.
Record emulation and physical hardware results separately in the release's
test report.
