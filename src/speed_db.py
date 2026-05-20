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
import time as _time

try:
    # db_signer is shipped alongside speed_db on the device (both in /root/).
    # If unavailable (e.g. running in a constrained recovery shell that
    # somehow lost the file) we degrade to "unsigned mode" which the
    # _ENFORCE_SIGNATURE check below treats as a hard reject by default.
    import db_signer as _signer
except ImportError:
    _signer = None

# Set to False in dev/recovery to skip signature enforcement entirely
# (mirrors the /etc/ai_hud_db_allow_unsigned escape hatch but lives
# in code so a test harness can flip it without touching the rootfs).
# Production firmware ships True.
_ENFORCE_SIGNATURE = True

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Header layouts -- writer always emits the current DB_VERSION; reader
# accepts every prior format for forward-compat. Old .db files keep
# working through firmware upgrades; we never assume the on-disk DB
# was produced by the same revision of this script.
#
# v1 (legacy, pre 2026-05-19):
#   header: magic, version, count, rec_size, flags (16 bytes)
#   record: lat_e6, lon_e6, speed, rec_type, bearing, grid_key (16 bytes)
# v2 (2026-05-19 +): same record, header gains build_epoch (20 bytes).
# v3 (2026-05-21 +): same header layout as v2; record grows to 18 bytes
#   with source_mask + confidence trailing the v2 layout so a v2 reader
#   over a v3 file would refuse on the rec_size check (defense in depth).
HEADER_FMT_V1 = "<4sHIHI"           # magic, version, count, rec_size, flags
HEADER_FMT_V2 = "<4sHIHII"          # ... + build_epoch (uint32 unix time)
HEADER_FMT_V3 = HEADER_FMT_V2       # header unchanged; only record grows
HEADER_SIZE_V1 = struct.calcsize(HEADER_FMT_V1)  # 16 bytes
HEADER_SIZE_V2 = struct.calcsize(HEADER_FMT_V2)  # 20 bytes
HEADER_SIZE_V3 = HEADER_SIZE_V2                  # 20 bytes
# Kept under the original name so existing imports don't break.
HEADER_FMT = HEADER_FMT_V3
HEADER_SIZE = HEADER_SIZE_V3

# v1+v2 record (16 bytes): lat_e6, lon_e6, speed, rec_type, bearing, grid_key
RECORD_FMT_V12 = "<iiBBHI"
RECORD_SIZE_V12 = struct.calcsize(RECORD_FMT_V12)
# v3 record (18 bytes): adds source_mask + confidence.
#   source_mask uint8 -- bit field; bit definitions in SRC_BIT_* below.
#   confidence  uint8 -- enum CONFIDENCE_* below.
RECORD_FMT_V3 = "<iiBBHIBB"
RECORD_SIZE_V3 = struct.calcsize(RECORD_FMT_V3)
# Legacy aliases used by writers that target the current version.
RECORD_FMT = RECORD_FMT_V3
RECORD_SIZE = RECORD_SIZE_V3

MAGIC_ZONES = b"SZON"
MAGIC_CAMERAS = b"SCAM"
DB_VERSION = 3

# Source mask bits. Stable -- never re-number. New sources get the
# next unused bit. bit 6 left open as "future state/territory".
SRC_BIT_VIC = 1 << 0
SRC_BIT_NSW = 1 << 1
SRC_BIT_QLD = 1 << 2
SRC_BIT_WA  = 1 << 3
SRC_BIT_SA  = 1 << 4
SRC_BIT_ACT = 1 << 5
SRC_BIT_NT  = 1 << 6   # reserved -- no open speed dataset as of 2026-05
SRC_BIT_OSM = 1 << 7

# Confidence enum. Order matters -- higher = trust more.
CONFIDENCE_SINGLE_SOURCE     = 0   # only one source covers this point
CONFIDENCE_CROSS_NO_OFFICIAL = 1   # >=2 non-gov sources agree (e.g. OSM + crowd)
CONFIDENCE_OFFICIAL_ONLY     = 2   # 1 gov source, no OSM corroboration
CONFIDENCE_OFFICIAL_VERIFIED = 3   # gov + OSM agree on the value
# Sentinel for v1/v2 records loaded by v3 reader: we don't know the
# source breakdown, so present "single source / unverified" to the
# fusion layer. Keeps the "宁可不报" guarantee on legacy DBs.
CONFIDENCE_UNKNOWN = CONFIDENCE_SINGLE_SOURCE
SRC_MASK_UNKNOWN = 0

# SpeedFusion.source values. Strings (not ints) because they surface
# in the UI status line and log lines unchanged. Centralizing them as
# constants stops a typo on either side of the producer/consumer pair
# from silently breaking the low-confidence display gate.
SOURCE_DB                 = "DB"
SOURCE_DB_LOW_CONFIDENCE  = "DB_LOW_CONFIDENCE"
SOURCE_NPU                = "NPU"
SOURCE_DEFAULT            = "DEFAULT"

# SpeedDB.last_reject_reason values. Used by hud_live's status line
# to explain why a DB came back empty.
REJECT_MISSING   = "missing"
REJECT_SIGNATURE = "signature"
REJECT_ROLLBACK  = "rollback"
REJECT_FORMAT    = "format"

# Grid size for spatial indexing (degrees).
# 0.005 deg ~ 550m lat, ~450m lon at -33 deg (Sydney).
# Neighborhood search (3x3) covers ~1.65km x ~1.35km.
GRID_RES = 0.005

# Camera alert radius (meters)
CAMERA_ALERT_RADIUS = 800

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
    """Lightweight record holder (avoids namedtuple import overhead).

    `source_mask` and `confidence` were added in DB_VERSION 3. Records
    loaded from v1/v2 files are filled with SRC_MASK_UNKNOWN /
    CONFIDENCE_UNKNOWN so downstream code (SpeedFusion) can apply the
    conservative "single source -> don't display number" policy.
    """
    __slots__ = ("lat", "lon", "speed", "rec_type", "bearing",
                 "grid_key", "source_mask", "confidence")

    def __init__(self, lat_e6, lon_e6, speed, rec_type, bearing,
                 grid_key, source_mask=SRC_MASK_UNKNOWN,
                 confidence=CONFIDENCE_UNKNOWN):
        self.lat = lat_e6 / 1_000_000.0
        self.lon = lon_e6 / 1_000_000.0
        self.speed = speed
        self.rec_type = rec_type
        self.bearing = bearing
        self.grid_key = grid_key
        self.source_mask = source_mask
        self.confidence = confidence


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
        # Latest build_epoch read from any loaded file; 0 means "unknown"
        # (v1 file, or no file loaded). Surfaced by the dashboard.
        self.build_epoch = 0
        # Last rejection reason (None when everything loaded cleanly).
        # Inspected by hud_live / dashboard to explain why the DB is
        # empty. Possible values: "missing", "signature", "rollback",
        # "format", None.
        self.last_reject_reason = None

        if zones_path and os.path.isfile(zones_path):
            if self._check_signature(zones_path):
                self._load(zones_path, MAGIC_ZONES, self._zone_grid)
        if cameras_path and os.path.isfile(cameras_path):
            if self._check_signature(cameras_path):
                self._load(cameras_path, MAGIC_CAMERAS, self._camera_grid)

    def _check_signature(self, path):
        """Verify HMAC + rollback gate before parsing the file.

        Returns True iff the file is safe to load. Refusal reasons are
        recorded in self.last_reject_reason so the operator can see
        why on the dashboard. Default policy is fail-closed: a missing
        signature OR a mismatch returns False unless the
        /etc/ai_hud_db_allow_unsigned escape hatch is present.
        """
        if _signer is None:
            # The signing module didn't import -- e.g. running unit
            # tests against an old checkout. Honour _ENFORCE_SIGNATURE
            # so tests can opt out by toggling the constant.
            if _ENFORCE_SIGNATURE:
                print(f"[speed_db] REJECT {path}: db_signer module "
                      f"unavailable and enforcement is on")
                self.last_reject_reason = REJECT_SIGNATURE
                return False
            return True

        if not _ENFORCE_SIGNATURE or _signer.allow_unsigned():
            return True

        # Rollback gate: reject before paying the HMAC cost. A forged
        # .sig over a stale .db would still pass HMAC -- the epoch
        # check is what catches that.
        epoch = _signer.read_build_epoch_from_header(path)
        min_epoch = _signer.read_min_epoch()
        if min_epoch and epoch < min_epoch:
            print(f"[speed_db] REJECT {path}: build_epoch {epoch} "
                  f"< accepted min {min_epoch} (rollback attempt)")
            self.last_reject_reason = REJECT_ROLLBACK
            return False

        key = _signer.load_key(device_path=_signer.DEVICE_SECRET_PATH)
        if not _signer.verify_file(path, key):
            print(f"[speed_db] REJECT {path}: signature mismatch or "
                  f".sig missing")
            self.last_reject_reason = "signature"
            return False

        # Accept -- bump the high-water mark so a later downgrade is
        # blocked. Only update when we actually moved forward in time.
        if epoch > min_epoch:
            _signer.write_min_epoch(epoch)
        return True

    def _load(self, path, expected_magic, grid):
        """Load binary database file into grid index.

        Accepts v1 (16-byte header / 16-byte record), v2 (20-byte
        header / 16-byte record, build_epoch added) and v3 (20-byte
        header / 18-byte record, source_mask + confidence added).
        Older .db files from prior firmware revisions keep loading;
        their records present as `confidence=CONFIDENCE_UNKNOWN` and
        the fusion layer treats them as single-source per "宁可不报".
        """
        with open(path, "rb") as f:
            # Read v1-sized prefix to peek at the version, then top
            # up to v2-sized header if needed.
            hdr_v1 = f.read(HEADER_SIZE_V1)
            if len(hdr_v1) < HEADER_SIZE_V1:
                return
            magic, version, count, rec_size, flags = struct.unpack(
                HEADER_FMT_V1, hdr_v1)
            if magic != expected_magic:
                print(f"[speed_db] WARNING: bad magic in {path}: {magic}")
                return

            # Decide expected record size by version and verify the
            # writer agreed. A mismatch means the file was produced by
            # an incompatible build -- refuse to load rather than risk
            # mis-aligned record reads.
            if version in (1, 2):
                expected_rec_size = RECORD_SIZE_V12
            elif version == 3:
                expected_rec_size = RECORD_SIZE_V3
            else:
                print(f"[speed_db] WARNING: unsupported DB version in "
                      f"{path}: {version}")
                return
            if rec_size != expected_rec_size:
                print(f"[speed_db] WARNING: record size mismatch in {path}: "
                      f"{rec_size} != {expected_rec_size} for v{version}")
                return

            if version == 1:
                build_epoch = 0
            else:
                # v2 + v3 share the same header layout.
                extra = f.read(HEADER_SIZE_V2 - HEADER_SIZE_V1)
                if len(extra) < 4:
                    return
                (build_epoch,) = struct.unpack("<I", extra)

            data = f.read(count * expected_rec_size)

        loaded = 0
        record_fmt = RECORD_FMT_V3 if version == 3 else RECORD_FMT_V12
        for i in range(count):
            offset = i * expected_rec_size
            if offset + expected_rec_size > len(data):
                break
            if version == 3:
                (lat_e6, lon_e6, speed, rtype, bearing, gk,
                 source_mask, confidence) = struct.unpack_from(
                    record_fmt, data, offset)
            else:
                lat_e6, lon_e6, speed, rtype, bearing, gk = struct.unpack_from(
                    record_fmt, data, offset)
                source_mask = SRC_MASK_UNKNOWN
                confidence = CONFIDENCE_UNKNOWN
            rec = _Record(lat_e6, lon_e6, speed, rtype, bearing, gk,
                          source_mask=source_mask, confidence=confidence)
            grid.setdefault(gk, []).append(rec)
            loaded += 1

        if expected_magic == MAGIC_ZONES:
            self._zone_count = loaded
        else:
            self._camera_count = loaded
        # Track the most recent build_epoch across loaded files; zones
        # and cameras may have different build times if rebuilt separately.
        if build_epoch > self.build_epoch:
            self.build_epoch = build_epoch

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
            int: speed limit in km/h, or 0 if no data available.

        Backwards-compatible signature -- callers that want the
        confidence / source breakdown should use
        `query_speed_limit_full()` instead.
        """
        speed, _conf, _mask = self.query_speed_limit_full(lat, lon, heading)
        return speed

    def query_speed_limit_full(self, lat, lon, heading=None):
        """Like query_speed_limit but also returns confidence + source_mask.

        Returns:
            (speed, confidence, source_mask) -- speed=0 when no data.
            confidence is CONFIDENCE_UNKNOWN for v1/v2 records (loaded
            from older .db files), allowing the fusion layer to apply
            the conservative single-source policy uniformly.
        """
        if not self._zone_grid:
            return 0, CONFIDENCE_UNKNOWN, SRC_MASK_UNKNOWN

        best_dist = float("inf")
        best_rec = None

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
                    best_rec = rec

        # Only trust result if within reasonable distance (~150m from road)
        if best_rec is None or best_dist > 150:
            return 0, CONFIDENCE_UNKNOWN, SRC_MASK_UNKNOWN
        return best_rec.speed, best_rec.confidence, best_rec.source_mask

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

    def query(self, lat, lon, heading=None, camera_radius_m=None):
        """Combined query returning speed limit and camera warnings.

        Args:
            lat, lon: GPS coordinates
            heading: travel direction (optional)
            camera_radius_m: override camera search radius (default: module constant)

        Returns:
            QueryResult with .speed_limit, .speed_confidence,
            .speed_source_mask, .cameras, .camera_ahead, .camera_dist.
            The two new confidence fields default to UNKNOWN for v1/v2
            DBs so existing callers (which only read .speed_limit) keep
            working unchanged.
        """
        speed, confidence, source_mask = self.query_speed_limit_full(
            lat, lon, heading)
        radius = camera_radius_m if camera_radius_m is not None else CAMERA_ALERT_RADIUS
        cameras = self.query_cameras(lat, lon, heading, radius_m=radius)

        camera_ahead = len(cameras) > 0
        camera_dist = cameras[0][0] if cameras else -1
        camera_speed = cameras[0][1].speed if cameras else 0

        return QueryResult(speed, cameras, camera_ahead, camera_dist,
                           camera_speed, confidence, source_mask)


class QueryResult:
    """Result of a combined speed_db query."""
    __slots__ = ("speed_limit", "cameras", "camera_ahead",
                 "camera_dist", "camera_speed",
                 "speed_confidence", "speed_source_mask")

    def __init__(self, speed_limit, cameras, camera_ahead,
                 camera_dist, camera_speed,
                 speed_confidence=CONFIDENCE_UNKNOWN,
                 speed_source_mask=SRC_MASK_UNKNOWN):
        self.speed_limit = speed_limit
        self.cameras = cameras
        self.camera_ahead = camera_ahead
        self.camera_dist = camera_dist        # meters to nearest, -1 if none
        self.camera_speed = camera_speed      # enforced speed at nearest camera
        # Confidence enum + source-mask bit field of the matched zone
        # record. Surfaced to SpeedFusion so it can apply the "single
        # source -> do not display number" policy.
        self.speed_confidence = speed_confidence
        self.speed_source_mask = speed_source_mask


# ---------------------------------------------------------------------------
# Fusion logic: state machine combining DB + NPU detection
# ---------------------------------------------------------------------------

# NPU fusion thresholds. Tuned to minimize false positives because a
# wrong limit displayed to the driver is worse than no detection (DB
# fallback or default keep the driver legally safe).
#
# Reference: postprocess.h BOX_THRESH = 0.65 (training F1-optimal=0.664).
# A box only reaches Python after passing BOX_THRESH, so any fusion
# threshold below 0.65 has no effect -- before 2026-05-19 the no-DB
# threshold was 0.60 and silently turned off the entire fusion guard
# in regions where the GPS DB has no coverage.
#
# Trade-offs by region:
#   * DB-covered (highway, AU/CN main roads): aggressive 0.80 -- if the
#     NPU is rejected the DB value is used, which is correct by design.
#   * No-DB (small roads, new construction): 0.75 -- NPU is the only
#     source; over-tightening risks falling back to default_limit, which
#     can be wrong.
NPU_CONFIDENCE_MIN = 0.80       # minimum confidence when DB has data
NPU_CONFIDENCE_NO_DB = 0.75     # minimum confidence when DB is silent
NPU_VOTE_REQUIRED = 4           # ~4 GPS cycles at 1 Hz; still <= sign visibility window
NPU_OVERRIDE_TIMEOUT = 20.0     # seconds before reverting to DB (longer = less flap)


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

    def __init__(self, default_limit=100,
                 confidence_min=None,
                 confidence_no_db=None,
                 vote_required=None,
                 override_timeout=None,
                 camera_alert_radius=None):
        self.default = default_limit

        # Configurable thresholds (fall back to module-level defaults)
        self.confidence_min = (confidence_min if confidence_min is not None
                               else NPU_CONFIDENCE_MIN)
        self.confidence_no_db = (confidence_no_db if confidence_no_db is not None
                                 else NPU_CONFIDENCE_NO_DB)
        self.vote_required = (vote_required if vote_required is not None
                              else NPU_VOTE_REQUIRED)
        self.override_timeout = (override_timeout if override_timeout is not None
                                 else NPU_OVERRIDE_TIMEOUT)
        self.camera_alert_radius = (camera_alert_radius
                                    if camera_alert_radius is not None
                                    else CAMERA_ALERT_RADIUS)

        # NPU voting state
        self._npu_candidate = 0       # value NPU is proposing
        self._npu_votes = 0           # consecutive matching detections
        self._npu_override = 0        # accepted NPU override value (0 = none)
        self._npu_override_time = 0.0 # when override was last confirmed
        self._last_db_limit = 0       # last known DB limit

        # Output
        self.effective_limit = default_limit
        self.source = SOURCE_DEFAULT  # SOURCE_DB / NPU / DEFAULT / DB_LOW_CONFIDENCE

    def update(self, db_limit, npu_limit, npu_confidence, now=None,
               db_confidence=None):
        """Update fusion state and return effective speed limit.

        Args:
            db_limit: speed limit from database (0 = unknown)
            npu_limit: speed limit from NPU detection (0 = no detection)
            npu_confidence: NPU confidence (0.0 - 1.0)
            now: current time (default: time.time())
            db_confidence: CONFIDENCE_* enum for db_limit. Optional --
                callers passing None get the legacy behaviour (DB is
                trusted regardless of source breakdown), matching v1/v2
                .db files that have no per-record confidence.

        Returns:
            int: effective speed limit in km/h. When db_confidence is
            CONFIDENCE_SINGLE_SOURCE (only OSM, no official source),
            the .source field becomes "DB_LOW_CONFIDENCE" and the
            caller is expected to render "--" rather than the numeric
            value (per "宁可不报").
        """
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
        # "Trustworthy DB" means a DB record exists AND its confidence
        # is at least OFFICIAL_ONLY. Below that threshold we treat the
        # DB lookup as silent so the NPU isn't forced to outscore a
        # value we don't trust ourselves.
        db_trustworthy = (db_limit > 0 and
                          (db_confidence is None or
                           db_confidence >= CONFIDENCE_OFFICIAL_ONLY))
        conf_threshold = (self.confidence_min if db_trustworthy
                          else self.confidence_no_db)

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
        if (npu_active and self._npu_votes >= self.vote_required):
            candidate = self._npu_candidate

            if db_trustworthy:
                # Trusted DB: NPU can only LOWER the limit (safer)
                if 0 < candidate < db_limit:
                    self._npu_override = candidate
                    self._npu_override_time = now
                    npu_accepted = True
                # If NPU matches DB, that's confirmation, not override
                elif candidate == db_limit:
                    pass
                # If NPU > DB, ignore (never relax limit from NPU)
            else:
                # No trustworthy DB: accept NPU as primary source
                self._npu_override = candidate
                self._npu_override_time = now
                npu_accepted = True

        # --- Check NPU override timeout ---
        if (self._npu_override > 0 and not npu_accepted
                and now - self._npu_override_time > self.override_timeout):
            # NPU hasn't re-confirmed within timeout -- revert
            self._npu_override = 0
            self._npu_votes = 0
            self._npu_candidate = 0

        # --- Determine effective limit ---
        if self._npu_override > 0:
            self.effective_limit = self._npu_override
            self.source = SOURCE_NPU
        elif db_trustworthy:
            self.effective_limit = db_limit
            self.source = SOURCE_DB
        elif db_limit > 0:
            # We have a DB hit but only at SINGLE_SOURCE confidence
            # (e.g. OSM-only segment). Keep the value internally so a
            # later cross-verify pipeline can elevate it, but tell the
            # UI not to render it as a number.
            self.effective_limit = db_limit
            self.source = SOURCE_DB_LOW_CONFIDENCE
        else:
            self.effective_limit = self.default
            self.source = SOURCE_DEFAULT

        return self.effective_limit

    def reset(self):
        """Reset all state (e.g., on GPS fix loss)."""
        self._npu_candidate = 0
        self._npu_votes = 0
        self._npu_override = 0
        self._npu_override_time = 0.0
        self._last_db_limit = 0
        self.effective_limit = self.default
        self.source = SOURCE_DEFAULT


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
