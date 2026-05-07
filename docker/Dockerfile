FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive
ENV TZ=Asia/Shanghai

# Luckfox SDK build dependencies
RUN apt-get update && apt-get install -y \
    git ssh make gcc gcc-multilib g++-multilib \
    module-assistant expect g++ gawk texinfo libssl-dev bison flex \
    fakeroot cmake unzip gperf autoconf device-tree-compiler \
    libncurses5-dev pkg-config bc python-is-python3 passwd openssl \
    openssh-server openssh-client vim file cpio rsync curl \
    android-tools-adb python3-pip python3-venv \
    sudo locales \
    && rm -rf /var/lib/apt/lists/*

# Set locale
RUN locale-gen en_US.UTF-8
ENV LANG=en_US.UTF-8

# Create non-root user for build (some SDK scripts dislike root)
RUN useradd -m -s /bin/bash builder && echo "builder ALL=(ALL) NOPASSWD:ALL" >> /etc/sudoers

WORKDIR /home/builder
USER builder
