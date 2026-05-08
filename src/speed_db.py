#!/usr/bin/env python3
"""Offline speed limit + camera database with GPS-based spatial lookup.

Provides two databases loaded from compact binary files:
  1. Speed zones  -- road segments with posted speed limits
  2. Speed cameras -- fixed/mobile camera locations

Designed for Luckfox Pico Ultra (ARM, 256MB RAM, Python 3.11).
No third-party dependencies.

Binary file format (both databases share the same layout):
  Header (16 bytes):
    magic:     4 bytes  (b'SZON' or b'SCAM')
    version:   uint16
    count:     uint32
    rec_size:  uint16
    flags:     uint32   (reserved)

  Records (sorted by grid_key for locality):
    lat_e6:    int32    (latitude * 1e6)
    lon_e6:    int32    (longitude * 1e6)
    speed:     uint8    (speed limit km/h)
    rec_type:  uint8    (road/camera type)
    bearing:   uint16   (degrees, 0xFFFF = bidirectional/unknown)
    grid_key:  uint32   (spatial bucket key)
  Total: 16 bytes per record
"""

import os
import struct
import math

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

HEADER_FMT = "<4sHIHI"          # magic, version, count, rec_size, flags
HEADER_SIZE = struct.calcsize(HEADER_FMT)  # 16 bytes
RECORD_FMT = "<iiBBHI"          # lat_e6, lon_e6, speed, type, bearing, grid_key
RECORD_SIZE = struct.calcsize(RECORD_FMT)  # 16 bytes

MAGIC_ZONES = b"SZON"
MAGIC_CAMERAS = b"SCAM"
DB_VERSION = 1

# Grid size for spatial indexing (degrees).
# 0.005 deg ~ 550m lat, ~450m lon at -33 deg (Sydney).
# Neighborhood search (3x3) covers ~1.65km x ~1.35km.
GRID_RES = 0.005

# Camera alert radius (meters)
CAMERA_ALERT_RADIUS = 800
CAMERA_WARN_RADIUS = 400

# Approximate meters-per-degree at Australian latitudes (~-28 to -38)
_M_PER_DEG_LAT = 111_320.0
_M_PER_DEG_LON_AT_33 = 111_320.0 * math.cos(math.radians(33))  # ~93,400

# Camera type enum (matches prepare_speed_db.py)
CAM_FIXED_SPEED = 0
CAM_RED_LIGHT = 1
CAM_MOBILE_ZONE = 2
CAM_AVG_SPEED = 3
CAM_REDLIGHT_SPEED = 4

# Road type enum
ROAD_MOTORWAY = 0
ROAD_TRUNK = 1
ROAD_PRIMARY = 2
ROAD_SECONDARY = 3
ROAD_RESIDENTIAL = 4
ROAD_OTHER = 5


# ---------------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------------

def _grid_key(lat, lon):
    """Compute integer grid bucket key from lat/lon."""
    gi = int(math.floor(lat / GRID_RES))
    gj = int(math.floor(lon / GRID_RES))
    # Pack into uint32: upper 16 bits = lat index (offset), lower 16 = lon index
    # Offset so that negative latitudes map to positive range
    gi_u = (gi + 32768) & 0xFFFF
    gj_u = (gj + 32768) & 0xFFFF
    return (gi_u << 16) | gj_u


def _grid_neighbors(lat, lon):
    """Return 9 grid keys (current cell + 8 neighbors)."""
    gi = int(math.floor(lat / GRID_RES))
    gj = int(math.floor(lon / GRID_RES))
    keys = []
    for di in (-1, 0, 1):
        for dj in (-1, 0, 1):
            ni = ((gi + di) + 32768) & 0xFFFF
            nj = ((gj + dj) + 32768) & 0xFFFF
            keys.append((ni << 16) | nj)
    return keys


def _haversine_m(lat1, lon1, lat2, lon2):
    """Approximate distance in meters (flat-earth, fast, <0.1% error at <10km)."""
    dlat = (lat2 - lat1) * _M_PER_DEG_LAT
    dlon = (lon2 - lon1) * _M_PER_DEG_LON_AT_33
    return math.sqrt(dlat * dlat + dlon * dlon)


def _bearing_diff(b1, b2):
    """Absolute difference between two bearings (0-180)."""
    diff = abs(b1 - b2) % 360
    return diff if diff <= 180 else 360 - diff


# ---------------------------------------------------------------------------
# Database record (named tuple equivalent without import)
# ---------------------------------------------------------------------------

class _Record:
    """Lightweight record holder (avoids namedtuple import overhead)."""
    __slots__ = ("lat", "lon", "speed", "rec_type", "bearing", "grid_key")

    def __init__(self, lat_e6, lon_e6, speed, rec_type, bearing, grid_key):
        self.lat = lat_e6 / 1_000_000.0
        self.lon = lon_e6 / 1_000_000.0
        self.speed = speed
        self.rec_type = rec_type
        self.bearing = bearing
        self.grid_key = grid_key


# ---------------------------------------------------------------------------
# SpeedDB -- main database class
# ---------------------------------------------------------------------------

class SpeedDB:
    """Offline speed limit + camera database with spatial lookup.

    Usage:
        db = SpeedDB("/root/data/speed_zones.db", "/root/data/speed_cameras.db")
        result = db.query(lat, lon, heading)
        # result.speed_limit  -- posted speed (0 if unknown)
        # result.cameras      -- list of (dist_m, camera_record)
        # result.camera_ahead -- True if camera within alert radius in travel direction
    """

    def __init__(self, zones_path=None, cameras_path=None):
        self._zone_grid = {}     # grid_key -> [_Record, ...]
        self._camera_grid = {}   # grid_key -> [_Record, ...]
        self._zone_count = 0
        self._camera_count = 0

        if zones_path and os.path.isfile(zones_path):
            self._load(zones_path, MAGIC_ZONES, self._zone_grid)
        if cameras_path and os.path.isfile(cameras_path):
            self._load(cameras_path, MAGIC_CAMERAS, self._camera_grid)

    def _load(self, path, expected_magic, grid):
        """Load binary database file into grid index."""
        with open(path, "rb") as f:
            hdr = f.read(HEADER_SIZE)
            if len(hdr) < HEADER_SIZE:
                return
            magic, version, count, rec_size, flags = struct.unpack(HEADER_FMT, hdr)
            if magic != expected_magic:
                print(f"[speed_db] WARNING: bad magic in {path}: {magic}")
                return
            if rec_size != RECORD_SIZE:
                print(f"[speed_db] WARNING: record size mismatch in {path}: "
                      f"{rec_size} != {RECORD_SIZE}")
                return

            data = f.read(count * RECORD_SIZE)

        loaded = 0
        for i in range(count):
            offset = i * RECORD_SIZE
            if offset + RECORD_SIZE > len(data):
                break
            lat_e6, lon_e6, speed, rtype, bearing, gk = struct.unpack_from(
                RECORD_FMT, data, offset)
            rec = _Record(lat_e6, lon_e6, speed, rtype, bearing, gk)
            grid.setdefault(gk, []).append(rec)
            loaded += 1

        if expected_magic == MAGIC_ZONES:
            self._zone_count = loaded
        else:
            self._camera_count = loaded

        print(f"[speed_db] Loaded {loaded} records from {os.path.basename(path)} "
              f"({len(grid)} buckets)")

    @property
    def zone_count(self):
        return self._zone_count

    @property
    def camera_count(self):
        return self._camera_count

    def query_speed_limit(self, lat, lon, heading=None):
        """Return posted speed limit for nearest road segment.

        Args:
            lat, lon: GPS coordinates (decimal degrees)
            heading: travel direction in degrees (optional, for disambiguation)

        Returns:
            int: speed limit in km/h, or 0 if no data available
        """
        if not self._zone_grid:
            return 0

        best_dist = float("inf")
        best_speed = 0

        for gk in _grid_neighbors(lat, lon):
            bucket = self._zone_grid.get(gk)
            if not bucket:
                continue
            for rec in bucket:
                dist = _haversine_m(lat, lon, rec.lat, rec.lon)
                if dist < best_dist:
                    # If heading is available and record has bearing, prefer
                    # road segments aligned with travel direction
                    if (heading is not None and rec.bearing != 0xFFFF
                            and dist < best_dist * 0.8):
                        bdiff = _bearing_diff(heading, rec.bearing)
                        if bdiff > 90:
                            continue  # wrong direction
                    best_dist = dist
                    best_speed = rec.speed

        # Only trust result if within reasonable distance (~150m from road)
        if best_dist > 150:
            return 0
        return best_speed

    def query_cameras(self, lat, lon, heading=None, radius_m=CAMERA_ALERT_RADIUS):
        """Return nearby cameras sorted by distance.

        Args:
            lat, lon: GPS coordinates
            heading: travel direction (optional, filters by direction)
            radius_m: search radius in meters

        Returns:
            list of (distance_m, _Record) sorted by distance
        """
        if not self._camera_grid:
            return []

        results = []
        for gk in _grid_neighbors(lat, lon):
            bucket = self._camera_grid.get(gk)
            if not bucket:
                continue
            for rec in bucket:
                dist = _haversine_m(lat, lon, rec.lat, rec.lon)
                if dist > radius_m:
                    continue
                # Filter by direction if heading is known
                if (heading is not None and rec.bearing != 0xFFFF):
                    bdiff = _bearing_diff(heading, rec.bearing)
                    if bdiff > 60:
                        continue  # camera facing other direction
                results.append((dist, rec))

        results.sort(key=lambda x: x[0])
        return results

    def query(self, lat, lon, heading=None):
        """Combined query returning speed limit and camera warnings.

        Returns:
            QueryResult with .speed_limit, .cameras, .camera_ahead, .camera_dist
        """
        speed = self.query_speed_limit(lat, lon, heading)
        cameras = self.query_cameras(lat, lon, heading)

        camera_ahead = len(cameras) > 0
        camera_dist = cameras[0][0] if cameras else -1
        camera_speed = cameras[0][1].speed if cameras else 0

        return QueryResult(speed, cameras, camera_ahead, camera_dist, camera_speed)


class QueryResult:
    """Result of a combined speed_db query."""
    __slots__ = ("speed_limit", "cameras", "camera_ahead",
                 "camera_dist", "camera_speed")

    def __init__(self, speed_limit, cameras, camera_ahead,
                 camera_dist, camera_speed):
        self.speed_limit = speed_limit
        self.cameras = cameras
        self.camera_ahead = camera_ahead
        self.camera_dist = camera_dist        # meters to nearest, -1 if none
        self.camera_speed = camera_speed      # enforced speed at nearest camera


# ---------------------------------------------------------------------------
# Fusion logic: state machine combining DB + NPU detection
# ---------------------------------------------------------------------------

# NPU must meet ALL of these to be considered:
NPU_CONFIDENCE_MIN = 0.60       # minimum single-frame confidence
NPU_VOTE_REQUIRED = 3           # consecutive matching detections needed
NPU_OVERRIDE_TIMEOUT = 30.0     # seconds before reverting to DB
# When DB has no data, NPU threshold is slightly relaxed:
NPU_CONFIDENCE_NO_DB = 0.55


class SpeedFusion:
    """State machine for fusing DB and NPU speed limit sources.

    Prevents flickering by requiring temporal consistency from NPU.
    DB is always the trusted baseline; NPU can only lower the limit
    (construction zones, temporary changes), never raise it.

    Usage:
        fusion = SpeedFusion()
        # Called once per GPS cycle (~1 Hz):
        limit = fusion.update(db_limit, npu_limit, npu_confidence)
    """

    def __init__(self, default_limit=100):
        self.default = default_limit

        # NPU voting state
        self._npu_candidate = 0       # value NPU is proposing
        self._npu_votes = 0           # consecutive matching detections
        self._npu_override = 0        # accepted NPU override value (0 = none)
        self._npu_override_time = 0.0 # when override was last confirmed
        self._last_db_limit = 0       # last known DB limit

        # Output
        self.effective_limit = default_limit
        self.source = "DEFAULT"       # "DB", "NPU", "DEFAULT"

    def update(self, db_limit, npu_limit, npu_confidence, now=None):
        """Update fusion state and return effective speed limit.

        Args:
            db_limit: speed limit from database (0 = unknown)
            npu_limit: speed limit from NPU detection (0 = no detection)
            npu_confidence: NPU confidence (0.0 - 1.0)
            now: current time (default: time.time())

        Returns:
            int: effective speed limit in km/h
        """
        import time as _time
        if now is None:
            now = _time.time()

        # Track DB baseline changes (new road segment)
        if db_limit > 0:
            if db_limit != self._last_db_limit:
                # Entered a new road segment -- reset NPU override
                self._npu_override = 0
                self._npu_votes = 0
                self._npu_candidate = 0
            self._last_db_limit = db_limit

        # --- NPU voting logic ---
        conf_threshold = (NPU_CONFIDENCE_MIN if db_limit > 0
                          else NPU_CONFIDENCE_NO_DB)

        npu_active = npu_limit > 0 and npu_confidence >= conf_threshold

        if npu_active:
            if npu_limit == self._npu_candidate:
                self._npu_votes += 1
            else:
                # New candidate -- reset vote counter
                self._npu_candidate = npu_limit
                self._npu_votes = 1

        # --- Check if NPU candidate qualifies as override ---
        # Only accept/refresh override when NPU is ACTIVELY detecting
        # this frame. Stale votes cannot refresh the timeout.
        npu_accepted = False
        if (npu_active and self._npu_votes >= NPU_VOTE_REQUIRED):
            candidate = self._npu_candidate

            if db_limit > 0:
                # DB has data: NPU can only LOWER the limit (safer)
                if 0 < candidate < db_limit:
                    self._npu_override = candidate
                    self._npu_override_time = now
                    npu_accepted = True
                # If NPU matches DB, that's confirmation, not override
                elif candidate == db_limit:
                    pass
                # If NPU > DB, ignore (never relax limit from NPU)
            else:
                # No DB data: accept NPU as primary source
                self._npu_override = candidate
                self._npu_override_time = now
                npu_accepted = True

        # --- Check NPU override timeout ---
        if (self._npu_override > 0 and not npu_accepted
                and now - self._npu_override_time > NPU_OVERRIDE_TIMEOUT):
            # NPU hasn't re-confirmed within timeout -- revert
            self._npu_override = 0
            self._npu_votes = 0
            self._npu_candidate = 0

        # --- Determine effective limit ---
        if self._npu_override > 0:
            self.effective_limit = self._npu_override
            self.source = "NPU"
        elif db_limit > 0:
            self.effective_limit = db_limit
            self.source = "DB"
        else:
            self.effective_limit = self.default
            self.source = "DEFAULT"

        return self.effective_limit

    def reset(self):
        """Reset all state (e.g., on GPS fix loss)."""
        self._npu_candidate = 0
        self._npu_votes = 0
        self._npu_override = 0
        self._npu_override_time = 0.0
        self._last_db_limit = 0
        self.effective_limit = self.default
        self.source = "DEFAULT"


def fuse_camera_warning(db_cameras, npu_camera_detected):
    """Determine if camera warning should be shown.

    DB camera proximity is always trusted (static positions).
    NPU camera detection requires no temporal voting because
    camera warnings are transient alerts, not persistent state.

    Args:
        db_cameras: list from SpeedDB.query_cameras()
        npu_camera_detected: bool from NPU DetectionState

    Returns:
        (show_warning: bool, distance_m: float, source: str)
    """
    db_has = len(db_cameras) > 0
    if db_has:
        dist = db_cameras[0][0]
        return (True, dist, "DB")
    if npu_camera_detected:
        return (True, -1, "NPU")
    return (False, -1, "")
