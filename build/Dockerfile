FROM debian:trixie-slim

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       live-build debootstrap ca-certificates curl gnupg \
       xorriso isolinux syslinux-common syslinux-utils \
       grub-pc-bin grub-efi-amd64-bin mtools dosfstools \
       squashfs-tools e2fsprogs fdisk python3 gzip xz-utils zstd \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /work
ENTRYPOINT ["/bin/bash", "/src/build/build-in-container.sh"]
