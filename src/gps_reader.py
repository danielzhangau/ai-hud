#!/usr/bin/env python3
"""GPS NMEA parser for Luckfox Pico Ultra HUD project.

Reads NMEA sentences from UART and outputs parsed GPS data.
Device: /dev/ttyS4, Baud: 9600
"""

import os
import sys
import time
import struct
import fcntl
import termios


def open_serial(device="/dev/ttyS4", baudrate=9600):
    """Open serial port using raw termios (no pyserial dependency)."""
    fd = os.open(device, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)

    # Get current settings
    attrs = termios.tcgetattr(fd)

    # Set baud rate
    baud_map = {9600: termios.B9600, 115200: termios.B115200}
    speed = baud_map.get(baudrate, termios.B9600)
    attrs[4] = speed  # ispeed
    attrs[5] = speed  # ospeed

    # Raw mode: 8N1, no flow control
    attrs[0] = 0  # iflag: no input processing
    attrs[1] = 0  # oflag: no output processing
    attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL  # cflag
    attrs[3] = 0  # lflag: no canonical, no echo
    attrs[6][termios.VMIN] = 0  # non-blocking
    attrs[6][termios.VTIME] = 10  # 1 second timeout

    termios.tcsetattr(fd, termios.TCSANOW, attrs)

    # Clear non-blocking flag after setup
    flags = fcntl.fcntl(fd, fcntl.F_GETFL)
    fcntl.fcntl(fd, fcntl.F_SETFL, flags & ~os.O_NONBLOCK)

    return fd


def nmea_checksum(sentence):
    """Verify NMEA checksum."""
    if "*" not in sentence:
        return False
    data, checksum = sentence.rsplit("*", 1)
    if data.startswith("$"):
        data = data[1:]
    calc = 0
    for c in data:
        calc ^= ord(c)
    try:
        return calc == int(checksum.strip(), 16)
    except ValueError:
        return False


def parse_lat_lon(raw_val, direction):
    """Convert NMEA lat/lon (ddmm.mmmm) to decimal degrees."""
    if not raw_val or not direction:
        return None
    try:
        raw = float(raw_val)
    except ValueError:
        return None

    # NMEA format: ddmm.mmmm (lat) or dddmm.mmmm (lon)
    if direction in ("N", "S"):
        degrees = int(raw / 100)
    else:
        degrees = int(raw / 100)
    minutes = raw - (degrees * 100)
    decimal = degrees + minutes / 60.0

    if direction in ("S", "W"):
        decimal = -decimal
    return decimal


def parse_gprmc(parts):
    """Parse $GPRMC sentence - position, speed, heading, date/time."""
    if len(parts) < 10:
        return None

    result = {"type": "RMC", "valid": parts[2] == "A"}
    if not result["valid"]:
        return result

    # Time: hhmmss.ss
    if parts[1]:
        t = parts[1]
        result["utc_time"] = f"{t[0:2]}:{t[2:4]}:{t[4:6]}"

    # Position
    result["latitude"] = parse_lat_lon(parts[3], parts[4])
    result["longitude"] = parse_lat_lon(parts[5], parts[6])

    # Speed (knots -> km/h)
    if parts[7]:
        try:
            result["speed_kmh"] = float(parts[7]) * 1.852
        except ValueError:
            pass

    # Heading
    if parts[8]:
        try:
            result["heading"] = float(parts[8])
        except ValueError:
            pass

    # Date: ddmmyy
    if parts[9]:
        d = parts[9]
        result["date"] = f"20{d[4:6]}-{d[2:4]}-{d[0:2]}"

    return result


def parse_gpgga(parts):
    """Parse $GPGGA sentence - fix quality, altitude, satellites."""
    if len(parts) < 10:
        return None

    result = {"type": "GGA"}

    # Fix quality: 0=invalid, 1=GPS, 2=DGPS
    try:
        result["fix_quality"] = int(parts[6])
    except (ValueError, IndexError):
        result["fix_quality"] = 0

    # Satellite count
    try:
        result["satellites"] = int(parts[7])
    except (ValueError, IndexError):
        result["satellites"] = 0

    # Altitude (meters)
    if parts[9]:
        try:
            result["altitude"] = float(parts[9])
        except ValueError:
            pass

    return result


def parse_gpvtg(parts):
    """Parse $GPVTG sentence - ground speed."""
    if len(parts) < 8:
        return None

    result = {"type": "VTG"}

    # Speed in km/h (field 7)
    if parts[7]:
        try:
            result["speed_kmh"] = float(parts[7])
        except ValueError:
            pass

    return result


class GPSState:
    """Aggregated GPS state from multiple NMEA sentences."""

    def __init__(self):
        self.valid = False
        self.latitude = None
        self.longitude = None
        self.speed_kmh = 0.0
        self.heading = 0.0
        self.altitude = 0.0
        self.satellites = 0
        self.fix_quality = 0
        self.utc_time = ""
        self.date = ""
        self.last_update = 0

    def update(self, parsed):
        if parsed is None:
            return

        t = parsed.get("type", "")

        if t == "RMC":
            self.valid = parsed.get("valid", False)
            if self.valid:
                if "latitude" in parsed and parsed["latitude"] is not None:
                    self.latitude = parsed["latitude"]
                if "longitude" in parsed and parsed["longitude"] is not None:
                    self.longitude = parsed["longitude"]
                if "speed_kmh" in parsed:
                    self.speed_kmh = parsed["speed_kmh"]
                if "heading" in parsed:
                    self.heading = parsed["heading"]
                if "utc_time" in parsed:
                    self.utc_time = parsed["utc_time"]
                if "date" in parsed:
                    self.date = parsed["date"]
                self.last_update = time.time()

        elif t == "GGA":
            self.fix_quality = parsed.get("fix_quality", 0)
            self.satellites = parsed.get("satellites", 0)
            if "altitude" in parsed:
                self.altitude = parsed["altitude"]

        elif t == "VTG":
            if "speed_kmh" in parsed:
                self.speed_kmh = parsed["speed_kmh"]

    def __str__(self):
        if not self.valid:
            return f"[NO FIX] Satellites: {self.satellites} | Searching..."
        return (
            f"[FIX] {self.date} {self.utc_time} UTC | "
            f"Lat: {self.latitude:.6f} Lon: {self.longitude:.6f} | "
            f"Speed: {self.speed_kmh:.1f} km/h | "
            f"Heading: {self.heading:.0f} | "
            f"Alt: {self.altitude:.0f}m | "
            f"Sat: {self.satellites} | Fix: {self.fix_quality}"
        )


def main():
    device = sys.argv[1] if len(sys.argv) > 1 else "/dev/ttyS4"
    baudrate = int(sys.argv[2]) if len(sys.argv) > 2 else 9600

    print(f"GPS Reader - Device: {device}, Baud: {baudrate}")
    print("Press Ctrl+C to stop\n")

    fd = open_serial(device, baudrate)
    gps = GPSState()
    buffer = b""

    parsers = {
        "RMC": parse_gprmc,
        "GGA": parse_gpgga,
        "VTG": parse_gpvtg,
    }

    try:
        while True:
            data = os.read(fd, 256)
            if not data:
                continue

            buffer += data
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                sentence = line.decode("ascii", errors="replace").strip()

                if not sentence.startswith("$"):
                    continue
                if not nmea_checksum(sentence):
                    continue

                parts = sentence.split("*")[0].split(",")
                msg_type = parts[0]

                # Match any talker (GP, GN, GL, etc.)
                for key, parser in parsers.items():
                    if msg_type.endswith(key):
                        result = parser(parts)
                        gps.update(result)
                        break

                # Print on RMC (once per GPS cycle)
                if msg_type.endswith("RMC"):
                    print(f"\r{gps}", end="", flush=True)

    except KeyboardInterrupt:
        print("\n\nStopped.")
    finally:
        os.close(fd)


if __name__ == "__main__":
    main()
