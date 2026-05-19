#!/usr/bin/env bash
# Build a small FAT32 disk image that the device exposes as a USB Mass
# Storage gadget. When the customer plugs the device into a Mac the OS
# auto-mounts this image -- they see a regular USB drive named "AI-HUD"
# containing the launcher zip and a quick-start README.
#
# macOS-only (uses newfs_msdos + hdiutil). Output:
#     dist/launcher.img      -- the raw FAT32 image (~32 MB)
#     dist/launcher.img.sha256
#
# Source files staged on the disk:
#     /AI-HUD Config.zip     -- the launcher distributable zip
#     /HOW-TO-OPEN.md        -- end-user instructions
#
# Run from repo root.

set -euo pipefail

SIZE_MB="${SIZE_MB:-32}"
OUT_DIR="${OUT_DIR:-dist}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
LAUNCHER_ZIP="$REPO_ROOT/mac-launcher/dist/AI-HUD Config.zip"
HOW_TO_MD="$REPO_ROOT/mac-launcher/HOW-TO-OPEN.md"

OUT_DIR_ABS="$(cd "$REPO_ROOT" && pwd)/$OUT_DIR"
mkdir -p "$OUT_DIR_ABS"
IMG="$OUT_DIR_ABS/launcher.img"

if [ ! -f "$LAUNCHER_ZIP" ]; then
    echo "ERROR: $LAUNCHER_ZIP not found. Build the launcher first:"
    echo "  cd mac-launcher && ./build.sh"
    exit 1
fi

# --- 1. Allocate raw image -------------------------------------------------
# Linux mass_storage expects a regular block-aligned image (filesystem
# straight on the disk, no partition table). We build it in three steps on
# macOS because the alternatives all have quirks: `newfs_msdos` refuses
# to operate on a plain file, `hdiutil create -format UDRW` requires a
# source folder, and creating a UDIF dmg first then converting introduces
# a partition table.
#
# So: dd a sparse file, hdiutil-attach it without mounting (lets us
# format it like a block device), newfs_msdos -F 32, detach.
echo "==> Allocating ${SIZE_MB} MB raw image at $IMG"
rm -f "$IMG"
dd if=/dev/zero of="$IMG" bs=1m count="$SIZE_MB" status=none

echo "==> Attaching as block device for formatting"
ATTACH_OUT="$(hdiutil attach -nomount -imagekey diskimage-class=CRawDiskImage "$IMG")"
RAW_DEV="$(echo "$ATTACH_OUT" | awk '{print $1; exit}')"
if [ -z "$RAW_DEV" ]; then
    echo "ERROR: hdiutil attach did not return a device node"
    echo "$ATTACH_OUT"
    exit 2
fi
# We use the raw character device (/dev/rdiskN) for newfs_msdos: orders
# of magnitude faster than the buffered /dev/diskN.
RAW_RDEV="${RAW_DEV/disk/rdisk}"
echo "    device: $RAW_DEV (raw $RAW_RDEV)"

cleanup_attach() {
    hdiutil detach "$RAW_DEV" -quiet \
        || hdiutil detach "$RAW_DEV" -force -quiet \
        || true
}
trap cleanup_attach EXIT

echo "==> Formatting as FAT16 (volume label: AIHUD)"
# FAT16 instead of FAT32: FAT32 needs >=65525 clusters which would force
# the disk past ~256 MB. For a 32 MB launcher disk FAT16 is the right fit,
# and both macOS and Windows mount it transparently with no extra clicks.
# -F 16      = FAT16
# -v AIHUD   = volume label (max 11 chars, uppercase)
# -c 2       = cluster size (2 * 512B = 1 KiB), tight enough for ~10 MB files
newfs_msdos -F 16 -v AIHUD -c 2 "$RAW_RDEV" >/dev/null

cleanup_attach
trap - EXIT

# --- 2. Mount, copy files, unmount ----------------------------------------
echo "==> Mounting image to copy launcher files"
MOUNT_INFO="$(hdiutil attach -nobrowse -mountpoint /Volumes/AIHUD \
    -imagekey diskimage-class=CRawDiskImage "$IMG")"
DEVICE="$(echo "$MOUNT_INFO" | awk '{print $1; exit}')"
MOUNTPOINT="/Volumes/AIHUD"

# Always unmount on exit, even if cp fails -- otherwise we leak the disk
# entry and subsequent runs spam Finder with phantom volumes.
cleanup() {
    if mount | grep -q "$MOUNTPOINT"; then
        hdiutil detach "$DEVICE" -quiet || hdiutil detach "$DEVICE" -force -quiet || true
    fi
}
trap cleanup EXIT

echo "==> Staging files onto $MOUNTPOINT"
# Suppress macOS AppleDouble sidecars ("._foo"). Without this every cp
# emits a hidden xattr file that Windows / Linux users see in the file
# listing -- visually noisy and confusing.
export COPYFILE_DISABLE=1
cp "$LAUNCHER_ZIP" "$MOUNTPOINT/AI-HUD Config.zip"
if [ -f "$HOW_TO_MD" ]; then
    cp "$HOW_TO_MD" "$MOUNTPOINT/HOW-TO-OPEN.md"
fi

# Belt-and-braces: also delete the standard macOS sentinel directories
# and any AppleDouble siblings the OS may have created anyway.
find "$MOUNTPOINT" -maxdepth 1 -name '._*' -exec rm -f {} + 2>/dev/null || true
rm -rf "$MOUNTPOINT/.Spotlight-V100" "$MOUNTPOINT/.fseventsd" \
       "$MOUNTPOINT/.Trashes"            2>/dev/null || true

echo "==> Unmounting"
hdiutil detach "$DEVICE" -quiet
trap - EXIT

# --- 4. Checksum -----------------------------------------------------------
SHA="$(shasum -a 256 "$IMG" | awk '{print $1}')"
echo "$SHA  $(basename "$IMG")" > "$IMG.sha256"

SIZE_ACTUAL="$(du -h "$IMG" | awk '{print $1}')"
echo
echo "==> Done."
echo "    Image:  $IMG ($SIZE_ACTUAL)"
echo "    SHA256: $SHA"
echo
echo "Push to device:"
echo "  adb push '$IMG' /userdata/launcher.img"
