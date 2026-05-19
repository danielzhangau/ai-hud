# Firmware Update -- Detailed Procedure

This document covers the *firmware* flash path (kernel + rootfs, via
MaskROM), not the day-to-day OTA bundle path. For OTA see
`docs/update-bundle.md`; for the end-user-facing summary see
`docs/customer-journey.md`.

## When firmware updates are needed

| Triggering change | Firmware update needed? |
|---|---|
| Python module change (`hud_live.py` etc.) | No -- OTA bundle |
| `ai-hud` C binary change | Usually no -- OTA can push it |
| New / changed init script (`/etc/init.d/S99...`) | No -- OTA pushes those too |
| Speed database refresh | No -- OTA |
| `S50usbdevice` change (USB gadget descriptor) | Reboot enough, OTA covers it |
| **Kernel config / Linux version** | **Yes** -- new `boot.img` |
| **Rootfs layout / busybox / system libraries** | **Yes** -- new `rootfs.img` |
| **NCM / new USB gadget functions** | **Yes** -- kernel module |

Practically: kernel-level changes happen on the order of once per quarter.

## Mechanics

The Luckfox Pico Ultra has three USB-related boot states:

| State | idProduct | Behaviour |
|---|---|---|
| Linux running normally | `0x0019` | adb / mass_storage / NCM gadget visible |
| U-Boot loader | `0x110a` | Brief moment between MaskROM and Linux |
| **MaskROM** | `0x110b` | SoC mask ROM running, ready to accept full firmware |

MaskROM is the recovery path -- it's burned into the SoC at silicon
fabrication and **cannot be erased**. Holding the **BOOT** button on
the board while power-on tells the SoC to ignore eMMC and stay in
MaskROM, regardless of what's installed.

This is the only way to do a non-broken-by-design firmware flash: we
need the device to *not* be running Linux while we overwrite the
partitions Linux booted from.

## Procedure (macOS)

The launcher's `flash_firmware.py` does this end-to-end. Here's what
it does step by step.

### Step 1 -- Customer puts device in MaskROM

1. Unplug USB from the device
2. Press and hold the **BOOT** button on the board
3. While still holding BOOT, plug USB back in
4. Hold for ~2 more seconds, then release BOOT

The display will stay blank -- there's no visible feedback that
MaskROM is engaged. The customer just trusts the timing.

### Step 2 -- Customer double-clicks AI-HUD Config.app

The launcher's `launch.sh` runs:

```bash
ioreg -p IOUSB -l | awk '/USB Vendor Name.*rockchip/...'
```

Reads the `idProduct` of any present Rockchip device. If it sees
`0x110b` it routes to `flash_firmware.py` instead of the normal
OTA path.

### Step 3 -- Confirmation dialog

`flash_firmware.py` opens an `osascript` modal:

> AI-HUD device detected in firmware-flash mode.
>
> Flashing firmware:
>   - downloads ~450 MB (if not cached)
>   - replaces the kernel and root filesystem
>   - reboots the device
>
> Total time: 5-10 minutes. Do not unplug USB during this.
>
> Continue?

Cancel here is a safe no-op -- nothing on the device has changed.

### Step 4 -- Bundle fetch

The launcher hits `https://api.github.com/repos/danielzhangau/ai-hud/releases/latest`,
finds the `update.img` asset, and downloads it to
`~/Library/Caches/AI-HUD/update-<version>-<sha>.img`. SHA-256 is
verified against the `SHA256SUMS` file in the same release.

If a prior flash already cached the same version, it skips the download
and re-verifies. Switching between versions just adds a new cache
entry; old ones are kept for fast rollback.

### Step 5 -- upgrade_tool

`flash_firmware.py` then runs:

```bash
osascript -e 'do shell script "
    upgrade_tool UF /path/to/cached/update.img
" with administrator privileges'
```

The standard macOS authorization dialog asks for the user's login
password. Once they enter it, `upgrade_tool` is run with root
privileges (it needs raw USB access to talk to MaskROM, which is
denied to non-root processes).

Inside, `upgrade_tool UF` performs:

1. Probe the MaskROM device, get its flash info
2. Push a tiny in-memory loader (boot ROM doesn't know FAT/eMMC layout)
3. Hand the loader the new firmware image
4. Loader writes each partition (boot, rootfs, oem, ...) sequentially
5. Issues a reset

Total wall time: 3-5 minutes on a USB 2.0 host, fast eMMC.

### Step 6 -- Device reboots

The device drops off the USB bus when `upgrade_tool` issues reset.
The launcher's `_wait_for_device` polls `adb devices` for up to
60 seconds for it to come back. Typically it's back in 25-30s.

At this point the device is on **new firmware but stock rootfs**: 
there's no `/root/version.txt`, no Python business code, no
databases.

### Step 7 -- OTA bundle to catch up

The launcher doesn't run the OTA path automatically because of the
inevitable boot-up race condition (USB enumerating before hud_live
binds port 80). It just notifies the customer:

> Firmware vX.Y.Z flashed successfully.
> The device will reboot in a moment. Once it's back online the
> launcher will offer to push the matching code update.

The customer double-clicks the launcher again. This time it sees:

- ADB device (PID 0x0019) -- normal Linux mode
- `/root/version.txt` missing → treated as `0.0.0`
- GitHub latest = `vX.Y.Z`

OTA dialog appears, customer accepts, the rest of the system code
flows in, and ~45s later the device is on the matching v0.x.y bundle.

## Procedure (Windows)

Not implemented yet. Reasons:

1. Rockchip's USB raw-access driver on Windows is not signed for
   automatic install; the customer would have to OK an unsigned
   driver in Device Manager, breaking the "double-click only" promise.
2. `upgrade_tool.exe` exists, but its Windows-specific quirks
   (different DLL set, separate driver, slightly different command
   syntax) introduce a per-platform branch in the code we don't
   want to maintain until the customer mix demands it.

When a Windows-only customer reports needing a firmware update,
the supported path is:

- Borrow a Mac for ten minutes, or
- Send the device back to the developer for a flash service.

This is a rare ask -- once per device per several months -- so it
hasn't justified the engineering investment.

## Failure modes

| Symptom | Cause | Recovery |
|---|---|---|
| `upgrade_tool LD` says no MaskROM device | Customer didn't hold BOOT before plugging in | Unplug, press BOOT first, replug |
| MaskROM detected, flash starts, then "Download Boot Fail" | macOS denied USB access | Re-run with admin password actually entered |
| Flash gets to 90% then errors out | USB cable disconnected (vibration?) | Re-enter MaskROM, try again -- partition state is undefined but MaskROM still works |
| Device boots but ADB never appears | Bad firmware image or wrong partition layout | Re-flash with the previous known-good version |
| Customer can't enter MaskROM (BOOT button broken / unreachable in housing) | Hardware issue | Service-return path; no software workaround possible |

The MaskROM safety net is what makes firmware updates relatively
low-risk: nothing the customer can do, short of physical damage to
the BOOT pin, can make the device unrecoverable.

## What's *not* in this document

- Detailed flow of `upgrade_tool UF` internals -- read Rockchip's own
  documentation if you need to debug that level.
- How CI produces the firmware images -- see `docs/dev-workflow.md`.
- The OTA bundle format -- see `docs/update-bundle.md`.
- Hardware-level BOOT button location -- see `docs/hardware-reference.md`.
