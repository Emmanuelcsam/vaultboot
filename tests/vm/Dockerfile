FROM debian:trixie-slim
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
    qemu-system-x86 qemu-utils ovmf python3 ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /test
