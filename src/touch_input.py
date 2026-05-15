"""Non-blocking GT911 touch input via I2C userspace for ai-hud settings UI.

Reads GT911 touch registers directly from /dev/i2c-3 (no kernel evdev driver
needed). The Goodix kernel driver has a reset-sequence bug on this board that
kills I2C communication, so we bypass it entirely.

GT911 register map (16-bit big-endian address):
  0x8140-0x8143: Product ID ("911\0")
  0x814E:        Touch status (bit7=ready, bits3:0=touch count)
  0x814F-0x8156: Touch point 1 (track_id, x_lo, x_hi, y_lo, y_hi, size_lo, size_hi)
  0x8157-0x815E: Touch point 2 ...

Gesture recognition (simple state machine):
  - tap:        displacement < 20px, duration < 500ms
  - long_press: displacement < 20px, duration >= 800ms
  - swipe_left / swipe_right: X displacement > 50px

Usage:
    touch = TouchInput()          # opens /dev/i2c-3
    events = touch.poll()         # non-blocking, call from main loop
    for ev in events:
        print(ev.gesture, ev.x, ev.y)
    touch.close()
"""

import fcntl
import os
import time

# ---------------------------------------------------------------------------
# I2C constants
# ---------------------------------------------------------------------------

I2C_SLAVE = 0x0703
_GT911_ADDR = 0x5D        # GT911 default address on this board
_GT911_ADDR_ALT = 0x14    # alternate address (try if 0x5D fails)
_I2C_BUS = "/dev/i2c-3"

# GPIO sysfs paths for GT911 reset sequence
_GPIO_SYSFS = "/sys/class/gpio"
_GPIO_INT = 3     # GPIO0_A3 = IRQ pin (controls I2C address on reset)
_GPIO_RST = 4     # GPIO0_A4 = RESET pin

# GT911 registers (16-bit big-endian)
_REG_PRODUCT_ID = bytes([0x81, 0x40])
_REG_TOUCH_STATUS = bytes([0x81, 0x4E])
_REG_TOUCH_DATA = bytes([0x81, 0x4F])  # first touch point
_TOUCH_POINT_SIZE = 8     # bytes per touch point
_REG_CLEAR_STATUS = bytes([0x81, 0x4E])

# Screen coordinate range (GT911 reports in panel native resolution)
# Will be auto-detected from config register, default to 480x480
_DEFAULT_MAX_X = 480
_DEFAULT_MAX_Y = 480

# Target screen resolution (for coordinate mapping)
SCREEN_W = 480
SCREEN_H = 480

# Gesture detection thresholds
_TAP_MAX_DISPLACEMENT = 20    # pixels
_TAP_MAX_DURATION = 0.5       # seconds
_LONG_PRESS_DURATION = 0.8    # seconds
_SWIPE_MIN_DISPLACEMENT = 50  # pixels

# Polling interval (GT911 reports at ~60Hz, we don't need that fast)
_POLL_INTERVAL = 0.05  # 50ms = 20Hz max
_I2C_RESET_THRESHOLD = 10  # consecutive OSErrors before hardware reset

# GT911 stall recovery thresholds.
# GT911 scans at ~60Hz; even with no touch, buffer_ready toggles every
# ~17ms.  If buffer_ready stays 0 for STALL_DETECT_S, the firmware has
# hung and we do a hardware reset.  STALL_COOLDOWN_S prevents rapid
# consecutive resets which destabilize GT911.
_STALL_DETECT_S = 2.0    # seconds of no buffer_ready before hw reset
_STALL_COOLDOWN_S = 30.0  # minimum interval between resets

# Init retry: GT911 may not respond immediately after hardware reset.
# 5 retries x 200ms = up to 1s extra wait (on top of 500ms calibration).
_INIT_RETRIES = 5
_INIT_RETRY_DELAY = 0.2   # seconds between retries


# ---------------------------------------------------------------------------
# TouchEvent result
# ---------------------------------------------------------------------------

class TouchEvent:
    """A recognized touch gesture."""
    __slots__ = ("gesture", "x", "y", "duration")

    def __init__(self, gesture, x, y, duration=0.0):
        self.gesture = gesture    # "tap", "long_press", "swipe_left", "swipe_right"
        self.x = x                # X coordinate (screen pixels)
        self.y = y                # Y coordinate (screen pixels)
        self.duration = duration  # seconds held

    def __repr__(self):
        return (f"TouchEvent({self.gesture}, x={self.x}, y={self.y}, "
                f"dur={self.duration:.2f}s)")


# ---------------------------------------------------------------------------
# TouchInput (I2C userspace)
# ---------------------------------------------------------------------------

class TouchInput:
    """Non-blocking GT911 touch reader via I2C with gesture recognition."""

    def __init__(self, i2c_bus=None, addr=None):
        """Open I2C bus and verify GT911 communication.

        Args:
            i2c_bus: path like "/dev/i2c-3", or None for default.
            addr: GT911 I2C address, or None for auto-detect (0x5D then 0x14).
        """
        self._fd = -1
        self._addr = 0
        self._max_x = _DEFAULT_MAX_X
        self._max_y = _DEFAULT_MAX_Y
        self._last_poll = 0.0

        bus = i2c_bus or _I2C_BUS

        # Hardware reset GT911 with correct address selection (0x5D).
        # Goodix kernel driver is disabled in device tree.
        self._hw_reset()

        try:
            self._fd = os.open(bus, os.O_RDWR)
        except OSError as e:
            print(f"[touch] WARNING: cannot open {bus}: {e}")
            return

        # Auto-detect address with retries.
        # GT911 may need extra time after hardware reset before Product ID
        # reads succeed -- retry up to _INIT_RETRIES times with short delays
        # to handle variable startup latency.
        if addr:
            addrs_to_try = [addr]
        else:
            addrs_to_try = [_GT911_ADDR, _GT911_ADDR_ALT]

        for attempt in range(_INIT_RETRIES):
            for try_addr in addrs_to_try:
                try:
                    fcntl.ioctl(self._fd, I2C_SLAVE, try_addr)
                    os.write(self._fd, _REG_PRODUCT_ID)
                    pid = os.read(self._fd, 4)
                    if pid[:3] == b"911":
                        self._addr = try_addr
                        if attempt > 0:
                            print(f"[touch] GT911 found at 0x{try_addr:02X}"
                                  f" on {bus} (after {attempt} retries)")
                        else:
                            print(f"[touch] GT911 found at 0x{try_addr:02X}"
                                  f" on {bus}")
                        break
                except OSError:
                    continue
            if self._addr != 0:
                break
            if attempt < _INIT_RETRIES - 1:
                time.sleep(_INIT_RETRY_DELAY)

        if self._addr == 0:
            print(f"[touch] WARNING: GT911 not found on {bus}"
                  f" after {_INIT_RETRIES} attempts")
            os.close(self._fd)
            self._fd = -1
            return

        # Read config to get actual resolution
        self._read_resolution()

        # Touch state machine
        self._touch_down = False
        self._start_x = 0
        self._start_y = 0
        self._start_time = 0.0
        self._cur_x = 0
        self._cur_y = 0

        # I2C error recovery
        self._i2c_errors = 0

        # Stall detection: GT911 firmware bug can cause buffer_ready to
        # stay 0 indefinitely while I2C still works.  Only trigger when
        # touch was recently active -- GT911 legitimately stops scanning
        # in idle (no touch) low-power mode, which is NOT a stall.
        self._last_br1_time = time.time()
        self._last_touch_time = 0.0   # last time touch_count > 0
        self._last_reset_time = 0.0

        # Non-blocking reset state machine (for runtime recovery).
        # _reset_phase: 0=idle, 1=RST low wait, 2=RST high wait,
        #               3=calibration wait, 4=done (re-init I2C)
        self._reset_phase = 0
        self._reset_phase_time = 0.0

    @property
    def available(self):
        """Whether GT911 was successfully detected."""
        return self._fd >= 0 and self._addr != 0

    def poll(self):
        """Non-blocking read of touch status.

        Should be called from main loop. Returns list of completed gestures.
        Rate-limited to avoid excessive I2C traffic.

        During a non-blocking hardware reset (stall/error recovery), this
        method advances the GPIO state machine in ~50ms increments instead
        of blocking the main loop for 660ms.
        """
        if not self.available and self._reset_phase == 0:
            return []

        now = time.time()
        if now - self._last_poll < _POLL_INTERVAL:
            return []
        self._last_poll = now

        # ---- Non-blocking reset state machine (runtime recovery) ----
        if self._reset_phase > 0:
            return self._advance_reset(now)

        gestures = []

        try:
            # Read touch status register
            fcntl.ioctl(self._fd, I2C_SLAVE, self._addr)
            os.write(self._fd, _REG_TOUCH_STATUS)
            status_byte = os.read(self._fd, 1)[0]

            buffer_ready = (status_byte & 0x80) != 0
            touch_count = status_byte & 0x0F

            if not buffer_ready:
                # GT911 stall detection -- only when touch was recently
                # active.  GT911 enters low-power idle when untouched,
                # legitimately producing buffer_ready=0 indefinitely.
                # A real stall is: user is touching but GT911 stops
                # reporting.
                stall_dur = now - self._last_br1_time
                touch_recent = (now - self._last_touch_time
                                < _STALL_DETECT_S * 2)
                if (touch_recent
                        and stall_dur > _STALL_DETECT_S
                        and now - self._last_reset_time > _STALL_COOLDOWN_S):
                    print(f"[touch] GT911 stall ({stall_dur:.1f}s),"
                          " starting non-blocking hw reset")
                    self._last_reset_time = now
                    self._touch_down = False
                    self._begin_async_reset(now)
                return []

            self._last_br1_time = now

            if touch_count > 0 and touch_count <= 5:
                self._last_touch_time = now
                # Read first touch point (we only use single-touch)
                os.write(self._fd, _REG_TOUCH_DATA)
                data = os.read(self._fd, _TOUCH_POINT_SIZE)

                if len(data) >= 6:
                    # track_id = data[0]
                    raw_x = data[1] | (data[2] << 8)
                    raw_y = data[3] | (data[4] << 8)

                    # Map to screen coordinates
                    x = int(raw_x * SCREEN_W / self._max_x) if self._max_x > 0 else raw_x
                    y = int(raw_y * SCREEN_H / self._max_y) if self._max_y > 0 else raw_y
                    x = max(0, min(SCREEN_W - 1, x))
                    y = max(0, min(SCREEN_H - 1, y))

                    if not self._touch_down:
                        # New touch
                        self._touch_down = True
                        self._start_x = x
                        self._start_y = y
                        self._start_time = now
                    self._cur_x = x
                    self._cur_y = y

            elif self._touch_down and touch_count == 0:
                # Finger lifted
                gesture = self._resolve_gesture()
                if gesture:
                    print(f"[touch] {gesture}")
                    gestures.append(gesture)
                self._touch_down = False

            # Clear status register (must write 0 to acknowledge)
            os.write(self._fd, _REG_CLEAR_STATUS + b"\x00")

        except OSError:
            self._i2c_errors += 1
            if self._i2c_errors >= _I2C_RESET_THRESHOLD:
                self._i2c_errors = 0
                self._touch_down = False
                print("[touch] GT911 I2C errors -- starting non-blocking"
                      " hw reset")
                self._begin_async_reset(now)
            return []

        # Successful read -- reset error counter
        self._i2c_errors = 0
        return gestures

    def close(self):
        """Close I2C file descriptor and release GPIO pins."""
        if self._fd >= 0:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = -1
        # Unexport GPIOs only on final shutdown
        if TouchInput._gpio_ready:
            for gpio in (_GPIO_INT, _GPIO_RST):
                try:
                    with open(f"{_GPIO_SYSFS}/unexport", "w") as f:
                        f.write(str(gpio))
                except OSError:
                    pass
            TouchInput._gpio_ready = False

    # -----------------------------------------------------------------------
    # Internal
    # -----------------------------------------------------------------------

    _gpio_ready = False  # class-level: GPIOs exported once

    @classmethod
    def _ensure_gpio(cls):
        """Export GT911 GPIO pins once (idempotent).

        Keeps RST HIGH and INT as input after export so GT911 stays
        running.  Pins remain exported for the process lifetime to
        avoid unexport glitches that destabilize GT911.
        """
        if cls._gpio_ready:
            return
        for gpio in (_GPIO_INT, _GPIO_RST):
            try:
                with open(f"{_GPIO_SYSFS}/export", "w") as f:
                    f.write(str(gpio))
            except OSError:
                pass  # already exported
        try:
            # RST = output HIGH (keep GT911 running)
            with open(f"{_GPIO_SYSFS}/gpio{_GPIO_RST}/direction", "w") as f:
                f.write("out")
            with open(f"{_GPIO_SYSFS}/gpio{_GPIO_RST}/value", "w") as f:
                f.write("1")
            # INT = input (normal IRQ)
            with open(f"{_GPIO_SYSFS}/gpio{_GPIO_INT}/direction", "w") as f:
                f.write("in")
        except OSError as e:
            print(f"[touch] WARNING: GPIO init failed: {e}")
            return
        cls._gpio_ready = True

    @classmethod
    def _hw_reset(cls):
        """Hardware-reset GT911 via GPIO.

        The Goodix-TS kernel driver is disabled in device tree to prevent
        its broken reset sequence.  This method performs a proper GT911
        reset via GPIO to bring the chip online at address 0x5D.

        GT911 address selection protocol:
          - INT pin LOW  during RESET release -> address 0x5D
          - INT pin HIGH during RESET release -> address 0x14

        GPIOs are NOT unexported -- they stay exported to avoid pin
        glitches that would cause uncontrolled GT911 resets.
        """
        cls._ensure_gpio()

        try:
            # 1. Set INT=output LOW (selects address 0x5D)
            with open(f"{_GPIO_SYSFS}/gpio{_GPIO_INT}/direction", "w") as f:
                f.write("out")
            with open(f"{_GPIO_SYSFS}/gpio{_GPIO_INT}/value", "w") as f:
                f.write("0")

            # 2. Pull RESET low for 100ms
            with open(f"{_GPIO_SYSFS}/gpio{_GPIO_RST}/value", "w") as f:
                f.write("0")
            time.sleep(0.1)

            # 3. Release RESET while INT is still LOW
            with open(f"{_GPIO_SYSFS}/gpio{_GPIO_RST}/value", "w") as f:
                f.write("1")
            time.sleep(0.06)

            # 4. Set INT back to input (normal IRQ operation)
            with open(f"{_GPIO_SYSFS}/gpio{_GPIO_INT}/direction", "w") as f:
                f.write("in")
            time.sleep(0.5)  # 500ms: GT911 needs time for touch calibration

            print("[touch] GT911 hardware reset complete")
        except OSError as e:
            print(f"[touch] WARNING: GPIO reset failed: {e}")

    def _begin_async_reset(self, now):
        """Start a non-blocking GT911 hardware reset.

        Instead of blocking 660ms in the main loop, the reset is split
        into phases advanced by _advance_reset() on each poll() call:
          Phase 1: INT=LOW, RST=LOW  -> wait 100ms
          Phase 2: RST=HIGH          -> wait 60ms
          Phase 3: INT=input         -> wait 500ms (calibration)
          Phase 4: Re-init I2C and verify
        """
        self._ensure_gpio()
        try:
            # Phase 1: INT=output LOW, RST=LOW
            with open(f"{_GPIO_SYSFS}/gpio{_GPIO_INT}/direction", "w") as f:
                f.write("out")
            with open(f"{_GPIO_SYSFS}/gpio{_GPIO_INT}/value", "w") as f:
                f.write("0")
            with open(f"{_GPIO_SYSFS}/gpio{_GPIO_RST}/value", "w") as f:
                f.write("0")
            self._reset_phase = 1
            self._reset_phase_time = now
        except OSError as e:
            print(f"[touch] WARNING: async reset start failed: {e}")
            self._reset_phase = 0

    def _advance_reset(self, now):
        """Advance the non-blocking reset state machine. Returns []."""
        elapsed = now - self._reset_phase_time

        try:
            if self._reset_phase == 1 and elapsed >= 0.1:
                # Phase 2: Release RESET (INT still LOW)
                with open(f"{_GPIO_SYSFS}/gpio{_GPIO_RST}/value", "w") as f:
                    f.write("1")
                self._reset_phase = 2
                self._reset_phase_time = now

            elif self._reset_phase == 2 and elapsed >= 0.06:
                # Phase 3: Set INT back to input
                with open(f"{_GPIO_SYSFS}/gpio{_GPIO_INT}/direction",
                          "w") as f:
                    f.write("in")
                self._reset_phase = 3
                self._reset_phase_time = now

            elif self._reset_phase == 3 and elapsed >= 0.5:
                # Phase 4: Calibration done, verify communication
                self._reset_phase = 0
                self._last_br1_time = now
                print("[touch] GT911 non-blocking reset complete")

                # Verify GT911 responds and kick-start scan cycle.
                # GT911 won't set buffer_ready=1 until the host clears
                # the status register (write 0x00 to 0x814E) at least
                # once after reset.
                try:
                    fcntl.ioctl(self._fd, I2C_SLAVE, self._addr)
                    os.write(self._fd, _REG_PRODUCT_ID)
                    pid = os.read(self._fd, 4)
                    if pid[:3] == b"911":
                        # Clear status register to start scan cycle
                        os.write(self._fd,
                                 _REG_CLEAR_STATUS + b"\x00")
                        print("[touch] GT911 recovered successfully")
                        return []
                except OSError:
                    pass
                print("[touch] GT911 recovery failed -- touch disabled")
                self._addr = 0

        except OSError as e:
            print(f"[touch] WARNING: async reset phase"
                  f" {self._reset_phase} failed: {e}")
            self._reset_phase = 0

        return []

    def _read_resolution(self):
        """Read GT911 config register to get touch panel resolution."""
        try:
            # Config resolution at register 0x8048 (x_max) and 0x804A (y_max)
            os.write(self._fd, bytes([0x80, 0x48]))
            data = os.read(self._fd, 4)
            if len(data) >= 4:
                x_max = data[0] | (data[1] << 8)
                y_max = data[2] | (data[3] << 8)
                if 100 < x_max < 2000 and 100 < y_max < 2000:
                    self._max_x = x_max
                    self._max_y = y_max
                    print(f"[touch] GT911 resolution: {x_max}x{y_max}")
                    return
        except OSError:
            pass
        print(f"[touch] GT911 resolution: using default {self._max_x}x{self._max_y}")

    def _resolve_gesture(self):
        """Classify the completed touch as a gesture."""
        if self._start_time == 0.0:
            return None

        now = time.time()
        duration = now - self._start_time
        dx = self._cur_x - self._start_x
        dy = self._cur_y - self._start_y
        displacement = (dx * dx + dy * dy) ** 0.5

        # Reset state
        self._start_time = 0.0

        if displacement < _TAP_MAX_DISPLACEMENT:
            if duration < _TAP_MAX_DURATION:
                return TouchEvent("tap", self._start_x, self._start_y, duration)
            elif duration >= _LONG_PRESS_DURATION:
                return TouchEvent("long_press", self._start_x, self._start_y,
                                  duration)
        elif abs(dx) > _SWIPE_MIN_DISPLACEMENT and abs(dx) > abs(dy):
            gesture = "swipe_right" if dx > 0 else "swipe_left"
            return TouchEvent(gesture, self._start_x, self._start_y, duration)

        return None  # unrecognized gesture
