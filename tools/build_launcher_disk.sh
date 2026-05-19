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

# 64 MB is the smallest size at which FAT32 (>=65525 clusters) works with
# newfs_msdos's defaults, and we need ~20 MB for both launchers plus
# headroom for the HOW-TO docs and a future Linux launcher.
SIZE_MB="${SIZE_MB:-64}"
OUT_DIR="${OUT_DIR:-dist}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

MAC_ZIP="$REPO_ROOT/mac-launcher/dist/AI-HUD Config.zip"
WIN_ZIP="$REPO_ROOT/windows-launcher/dist/AI-HUD Config (Windows).zip"
HOW_TO_MD="$REPO_ROOT/mac-launcher/HOW-TO-OPEN.md"

OUT_DIR_ABS="$(cd "$REPO_ROOT" && pwd)/$OUT_DIR"
mkdir -p "$OUT_DIR_ABS"
IMG="$OUT_DIR_ABS/launcher.img"

if [ ! -f "$MAC_ZIP" ]; then
    echo "ERROR: $MAC_ZIP not found. Build the macOS launcher first:"
    echo "  cd mac-launcher && ./build.sh"
    exit 1
fi
# Windows launcher is optional -- skip cleanly if not built yet, so
# dev-machines without working Windows builds can still iterate.
HAVE_WIN_LAUNCHER=0
if [ -f "$WIN_ZIP" ]; then
    HAVE_WIN_LAUNCHER=1
else
    echo "Note: $WIN_ZIP not found -- launcher disk will be mac-only."
    echo "      Run windows-launcher/build.sh to add Windows support."
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

echo "==> Formatting as FAT32 (volume label: AIHUD)"
# FAT32 needs >=65525 clusters. With cluster size 2 (1 KiB) and 64 MB
# we hit ~65k clusters but the reserved+FAT areas eat a few hundred,
# putting us just under the threshold. Cluster size 1 (512 B) gives
# 131072 raw clusters which clears 65525 with margin to spare. The
# FAT32 table itself ends up at ~512 KiB, fine on a 64 MB volume.
newfs_msdos -F 32 -v AIHUD -c 1 "$RAW_RDEV" >/dev/null

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

# Per-platform subdirectories so the customer can tell at a glance
# which folder applies to their OS. The macOS Config.zip is at the
# top level too (this is the primary platform) so Mac users don't
# have to dig.
cp "$MAC_ZIP" "$MOUNTPOINT/AI-HUD Config.zip"
mkdir -p "$MOUNTPOINT/For macOS"
cp "$MAC_ZIP" "$MOUNTPOINT/For macOS/AI-HUD Config.zip"

if [ "$HAVE_WIN_LAUNCHER" = "1" ]; then
    mkdir -p "$MOUNTPOINT/For Windows"
    cp "$WIN_ZIP" "$MOUNTPOINT/For Windows/AI-HUD Config (Windows).zip"
fi

if [ -f "$HOW_TO_MD" ]; then
    cp "$HOW_TO_MD" "$MOUNTPOINT/HOW-TO-OPEN.md"
fi

# Belt-and-braces: delete macOS sentinel directories and AppleDouble
# sidecars at every depth. Earlier we only swept the root; subfolders
# like "For macOS/" still ended up with their own "._" siblings, which
# Windows users would see as visible junk.
find "$MOUNTPOINT" -name '._*' -exec rm -f {} + 2>/dev/null || true
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
