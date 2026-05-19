# AI-HUD Customer Journey

What the end user actually does, from opening the box to many months later.
Written from the customer's point of view, not the developer's.

## The five touchpoints

```
 1. Open box  ──►  2. Install in car  ──►  3. Use every day
                                                  │
                                                  │   (months pass)
                                                  ▼
                              4. (Optional) Configure / inspect
                                                  │
                                                  │   (rarely)
                                                  ▼
                              5. (Very rare) Firmware upgrade
```

Touchpoints 1-3 require **no computer at all** -- the HUD works
standalone as soon as the device gets power.

Touchpoints 4 and 5 happen only when the customer chooses to plug into
a Mac or Windows PC. They are optional.

---

## 1. Open box

What's in the box:

- The AI-HUD device (housing + display already assembled)
- One USB-C data cable (power + data, for both car and computer use)
- A printed quick-start card (URL of online instructions + a single
  sentence: "plug it in, drive")

**No software CD, no separate driver disc, no QR code to scan.** The
software the user might need is on the device itself.

## 2. Install in car

1. Mount the device where it fits in the cabin
2. Plug the USB-C cable into a powered port on the car (cigarette
   lighter adapter, USB-A → USB-C adapter, dedicated 12 V converter --
   anything that supplies 5 V over USB)
3. Plug the other end into the device
4. Done

When the car ignition is on, the device boots in ~30 s and starts
showing the HUD. There is no on/off button -- it tracks car power.

## 3. Use every day

```
Car start          → device powers on  → splash → HUD active
Get GPS fix        → speed, current limit, regional units appear
Drive into tunnel  → ISP auto-tunes for low light
Sunset             → device switches to night palette (GPS sun-angle)
Car stop           → device powers off cleanly with the car
```

**The customer does nothing.** No app on a phone, no Wi-Fi setup, no
account.

## 4. (Optional) Configure / inspect

Triggers for plugging into a computer:

- "I want to see if it's working / how many satellites it sees"
- "I just installed it -- need to set Mirror on/off based on whether
  it's reflected off the windshield"
- "I heard there's a new version"

### First time on a new computer

The customer plugs the device into their Mac or Windows PC. The OS
auto-mounts an **`AIHUD` volume** -- it looks like a 67 MB USB drive.
Inside:

```
AIHUD/
├─ AI-HUD Config.zip       (macOS launcher, also a shortcut at top level)
├─ For macOS/
│   └─ AI-HUD Config.zip
├─ For Windows/
│   └─ AI-HUD Config (Windows).zip
└─ HOW-TO-OPEN.md          (bilingual instructions)
```

The customer picks their OS folder, double-clicks the zip to extract,
then:

- **Mac**: right-click `AI-HUD Config.app` → *Open* → confirm
  Gatekeeper warning (one time only)
- **Windows**: extract → double-click `Run AI-HUD Config.bat` → if
  SmartScreen warns, click *More info* → *Run anyway* (one time only)

After this first launch, both OSes simply double-click to run.

### Every time

```
Plug device into PC
   ↓
Double-click the launcher
   ↓
(within ~5 seconds)
   ↓
Browser opens automatically →  AI-HUD Dashboard
   ↓
Customer sees:
  - GPS fix status + satellite count
  - Current speed limit (with source: NPU / DB / default)
  - NPU detection status + last detected sign
  - Day/night mode (auto)
  - Region (auto)
  - Speed DB freshness
  - One toggle: Mirror display
   ↓
Make changes (instant save), or just look
   ↓
Close browser, unplug device, put device back in car
```

### When there's an update

When the launcher starts, it also checks GitHub for the latest version.
If the device is behind, the customer sees a dialog like:

> A new AI-HUD version is available.
> Current: 0.1.0
> New: 0.2.0
> Update now? (about 30 seconds)

If they accept, the launcher downloads the new bundle, pushes the files,
reboots the device, then opens the browser. They never touch a
terminal. If they cancel, the launcher just opens the browser with the
current version -- they can update later.

## 5. (Very rare) Firmware upgrade

Firmware updates -- the kernel and root filesystem -- are not part of
the normal OTA cycle. They happen maybe once or twice a year, when
something low-level changes (new USB feature, kernel security patch,
etc.).

The flow is the same shape as the day-to-day case, with one extra step
at the start: the customer has to put the device into **firmware-flash
mode** with the BOOT button.

```
1. Unplug USB
2. Press and hold the BOOT button on the device
3. While still holding BOOT, plug USB back in
4. Hold for ~2 seconds, then release
   (the screen will stay blank -- this is normal)
5. Double-click AI-HUD Config.app (macOS only for now)
6. Dialog: "Device is in firmware-flash mode. Flash firmware?"
7. Click Flash, enter your Mac login password (admin privilege)
8. Wait 5-10 minutes -- progress dialog stays open
9. Device reboots into the new firmware automatically
10. Double-click the launcher again to let it push the matching code
```

**Today, firmware flashing requires a Mac.** Windows users with no Mac
available should contact support; we're holding off on Windows firmware
flashing until we can ship the Rockchip USB driver in a way that
doesn't break the "double-click only" promise.

## What can go wrong, and how to recover

| Symptom | Likely cause | Fix |
|---|---|---|
| HUD black on power-up | Wrong USB cable (power-only, not data) | Try a known data cable |
| AIHUD volume doesn't appear on PC | Same | Same |
| Launcher says "no AI-HUD device" | Device hasn't finished booting | Wait 30 s, retry |
| Launcher says "configuration server not responding" | hud_live still starting / crashed | Power-cycle device |
| Browser shows old data | Cached page | Refresh (Cmd-R / Ctrl-R) |
| Update fails partway | Network blip or device rebooted early | Re-run launcher, OTA is idempotent |
| Firmware flash fails midway | USB unplugged or password cancelled | Re-enter MaskROM (BOOT + replug), try again |

If a firmware flash leaves the device unbootable: MaskROM is hardware
and always works. Worst case, the customer's stock firmware can be
reflashed from the Luckfox site, then OTA brings them current.

## What we promise

- HUD works the second the car gets power. No setup.
- Updating is one double-click on either macOS or Windows.
- The launcher is shipped with the device itself; no separate download.
- Day/night, region, sun position -- automatic from GPS, no settings.
- The customer touches no terminal at any point in the lifecycle.
