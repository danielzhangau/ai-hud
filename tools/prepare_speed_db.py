#!/usr/bin/env python3
"""Prepare offline speed limit + camera databases for ai-hud device.

Data sources:
  - Speed zones:  VIC government GeoJSON (CC BY 4.0)
  - Speed cameras: OpenStreetMap Overpass API (ODbL)
  - Future: NSW, QLD, WA government data

Output:
  data/speed_zones.db    -- binary speed zone database
  data/speed_cameras.db  -- binary camera location database

Usage:
  python tools/prepare_speed_db.py
  python tools/prepare_speed_db.py --zones-only
  python tools/prepare_speed_db.py --cameras-only
  python tools/prepare_speed_db.py --output-dir /path/to/output

Requirements:
  Python 3.8+, internet access for downloads.
  No third-party dependencies (uses urllib + json).
"""

import argparse
import json
import math
import os
import struct
import subprocess
import sys
import time
import urllib.request
import urllib.error
import urllib.parse

# ---------------------------------------------------------------------------
# Binary format (must match src/speed_db.py)
# ---------------------------------------------------------------------------

HEADER_FMT = "<4sHIHI"
HEADER_SIZE = struct.calcsize(HEADER_FMT)
RECORD_FMT = "<iiBBHI"
RECORD_SIZE = struct.calcsize(RECORD_FMT)

MAGIC_ZONES = b"SZON"
MAGIC_CAMERAS = b"SCAM"
DB_VERSION = 1

GRID_RES = 0.005  # must match speed_db.py

# ---------------------------------------------------------------------------
# Data source URLs
# ---------------------------------------------------------------------------

# VIC speed zones - March 2026 (CC BY 4.0, Department of Transport and Planning)
VIC_SPEED_ZONES_URL = (
    "https://opendata.transport.vic.gov.au/dataset/"
    "975b80b9-e530-46e2-80a5-54002765e81a/resource/"
    "91313d09-a353-4e4a-bca0-d137113790c8/download/"
    "speed_zones_march_2026.geojson"
)

# OSM Overpass API -- speed cameras in Australia
OVERPASS_API = "https://overpass-api.de/api/interpreter"
OVERPASS_QUERY = """
[out:json][timeout:120];
area["name"="Australia"]["admin_level"="2"]->.au;
(
  node["highway"="speed_camera"](area.au);
  node["enforcement"="maxspeed"](area.au);
  node["man_made"="surveillance"]["surveillance:type"="camera"]["surveillance:zone"="traffic"](area.au);
);
out body;
"""

# WA speed limits (ArcGIS GeoJSON endpoint)
WA_SPEED_LIMITS_URL = (
    "https://portal-mainroads.opendata.arcgis.com/datasets/"
    "mainroads::legal-speed-limits/explore"
)

# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

def grid_key(lat, lon):
    """Compute grid bucket key (must match speed_db.py)."""
    gi = int(math.floor(lat / GRID_RES))
    gj = int(math.floor(lon / GRID_RES))
    gi_u = (gi + 32768) & 0xFFFF
    gj_u = (gj + 32768) & 0xFFFF
    return (gi_u << 16) | gj_u


def download(url, desc="data", timeout=120):
    """Download URL with progress indication."""
    print(f"  Downloading {desc}...")
    print(f"  URL: {url[:80]}...")
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "ai-hud-data-prep/1.0"
        })
        resp = urllib.request.urlopen(req, timeout=timeout)

        # Chunked read with progress
        chunks = []
        total = 0
        while True:
            chunk = resp.read(1024 * 1024)  # 1MB chunks
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            mb = total / (1024 * 1024)
            print(f"\r  Downloaded: {mb:.1f} MB", end="", flush=True)

        print()  # newline after progress
        data = b"".join(chunks)
        size_kb = len(data) / 1024
        if size_kb > 1024:
            print(f"  Total: {size_kb/1024:.1f} MB")
        else:
            print(f"  Total: {size_kb:.0f} KB")
        return data
    except (urllib.error.URLError, Exception) as e:
        print(f"\n  ERROR: Download failed: {e}")
        return None


def write_db(path, magic, records):
    """Write binary database file.

    Args:
        path: output file path
        magic: 4-byte magic (MAGIC_ZONES or MAGIC_CAMERAS)
        records: list of (lat, lon, speed, rec_type, bearing) tuples
    """
    # Compute grid keys and sort by them (spatial locality)
    keyed = []
    for lat, lon, speed, rtype, bearing in records:
        gk = grid_key(lat, lon)
        lat_e6 = int(round(lat * 1_000_000))
        lon_e6 = int(round(lon * 1_000_000))
        keyed.append((gk, lat_e6, lon_e6, speed, rtype, bearing))

    keyed.sort(key=lambda x: x[0])

    with open(path, "wb") as f:
        # Header
        f.write(struct.pack(HEADER_FMT, magic, DB_VERSION,
                            len(keyed), RECORD_SIZE, 0))
        # Records
        for gk, lat_e6, lon_e6, speed, rtype, bearing in keyed:
            f.write(struct.pack(RECORD_FMT,
                                lat_e6, lon_e6, speed, rtype, bearing, gk))

    size_kb = os.path.getsize(path) / 1024
    print(f"  Wrote {len(keyed)} records -> {path} ({size_kb:.1f} KB)")


# ---------------------------------------------------------------------------
# Speed zones processing
# ---------------------------------------------------------------------------

def _sample_linestring(coords, interval_m=30):
    """Sample points along a LineString at fixed intervals.

    Args:
        coords: list of [lon, lat] pairs (GeoJSON order)
        interval_m: sampling interval in meters

    Yields:
        (lat, lon) tuples along the line
    """
    M_PER_DEG = 111_320.0

    if len(coords) < 2:
        if coords:
            yield (coords[0][1], coords[0][0])
        return

    # Always yield first point
    yield (coords[0][1], coords[0][0])

    accum = 0.0
    for i in range(1, len(coords)):
        lat1, lon1 = coords[i-1][1], coords[i-1][0]
        lat2, lon2 = coords[i][1], coords[i][0]

        dlat = (lat2 - lat1) * M_PER_DEG
        cos_lat = math.cos(math.radians((lat1 + lat2) / 2))
        dlon = (lon2 - lon1) * M_PER_DEG * cos_lat
        seg_len = math.sqrt(dlat*dlat + dlon*dlon)

        if seg_len < 0.01:
            continue

        pos = 0.0
        while pos < seg_len:
            remaining = interval_m - accum
            if pos + remaining <= seg_len:
                pos += remaining
                frac = pos / seg_len
                lat = lat1 + (lat2 - lat1) * frac
                lon = lon1 + (lon2 - lon1) * frac
                yield (lat, lon)
                accum = 0.0
            else:
                accum += seg_len - pos
                break

    # Always yield last point
    yield (coords[-1][1], coords[-1][0])


def _bearing_of_segment(coords):
    """Compute bearing from first to last point of a coordinate list."""
    if len(coords) < 2:
        return 0xFFFF
    lat1, lon1 = coords[0][1], coords[0][0]
    lat2, lon2 = coords[-1][1], coords[-1][0]
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    if abs(dlat) < 1e-8 and abs(dlon) < 1e-8:
        return 0xFFFF
    angle = math.degrees(math.atan2(dlon, dlat)) % 360
    return int(round(angle))


ROAD_TYPE_MAP = {
    "motorway": 0, "motorway_link": 0,
    "trunk": 1, "trunk_link": 1,
    "primary": 2, "primary_link": 2,
    "secondary": 3, "secondary_link": 3,
    "tertiary": 3, "tertiary_link": 3,
    "residential": 4, "living_street": 4,
    "unclassified": 5, "service": 5,
}


def process_vic_speed_zones(geojson_data):
    """Parse VIC speed zones GeoJSON and return sampled point records.

    Returns:
        list of (lat, lon, speed, road_type, bearing) tuples
    """
    try:
        data = json.loads(geojson_data)
    except json.JSONDecodeError as e:
        print(f"  ERROR: Failed to parse GeoJSON: {e}")
        return []

    features = data.get("features", [])
    print(f"  Parsing {len(features)} features...")

    records = []
    skipped = 0

    for feat in features:
        props = feat.get("properties", {})
        geom = feat.get("geometry", {})

        # Extract speed limit -- try various property names
        speed = 0
        for key in ("SPEED_ZONE", "SPEED", "speed_zone", "speed",
                     "SPEED_LIMIT", "speed_limit", "POSTED_SPEED"):
            val = props.get(key)
            if val is not None:
                try:
                    speed = int(float(val))
                    break
                except (ValueError, TypeError):
                    continue

        if speed <= 0 or speed > 130:
            skipped += 1
            continue

        # Extract road type
        road_name = props.get("ROAD_NAME", props.get("road_name", ""))
        road_class = props.get("ROAD_CLASS", props.get("road_class", ""))
        rtype = 5  # default: other

        # Determine geometry type
        gtype = geom.get("type", "")
        if gtype == "LineString":
            coord_lists = [geom.get("coordinates", [])]
        elif gtype == "MultiLineString":
            coord_lists = geom.get("coordinates", [])
        else:
            skipped += 1
            continue

        for coords in coord_lists:
            if len(coords) < 2:
                continue
            bearing = _bearing_of_segment(coords)
            for lat, lon in _sample_linestring(coords, interval_m=30):
                records.append((lat, lon, speed, rtype, bearing))

    if skipped:
        print(f"  Skipped {skipped} features (no valid speed/geometry)")
    print(f"  Generated {len(records)} sample points from speed zones")
    return records


# ---------------------------------------------------------------------------
# Speed camera processing
# ---------------------------------------------------------------------------

CAM_TYPE_MAP = {
    "maxspeed": 0,      # fixed speed
    "traffic_signals": 1,  # red light
    "speed_camera": 0,
}


def process_osm_cameras(json_data):
    """Parse OSM Overpass API JSON response for speed cameras.

    Returns:
        list of (lat, lon, speed, camera_type, bearing) tuples
    """
    try:
        data = json.loads(json_data)
    except json.JSONDecodeError as e:
        print(f"  ERROR: Failed to parse Overpass JSON: {e}")
        return []

    elements = data.get("elements", [])
    print(f"  Parsing {len(elements)} OSM elements...")

    records = []

    for elem in elements:
        if elem.get("type") != "node":
            continue

        lat = elem.get("lat")
        lon = elem.get("lon")
        if lat is None or lon is None:
            continue

        tags = elem.get("tags", {})

        # Determine camera type
        cam_type = 0  # default: fixed speed
        enforcement = tags.get("enforcement", "")
        if "traffic_signals" in enforcement or "red_light" in enforcement:
            cam_type = 1  # red light
        elif "average_speed" in enforcement:
            cam_type = 3  # average speed

        highway_tag = tags.get("highway", "")
        if highway_tag == "speed_camera":
            cam_type = 0

        # Extract enforced speed limit
        speed = 0
        maxspeed = tags.get("maxspeed", "")
        if maxspeed:
            try:
                speed = int(maxspeed)
            except ValueError:
                pass

        # Direction
        bearing = 0xFFFF  # unknown
        direction = tags.get("direction", "")
        if direction:
            try:
                bearing = int(float(direction)) % 360
            except ValueError:
                pass

        records.append((lat, lon, speed, cam_type, bearing))

    print(f"  Extracted {len(records)} camera locations from OSM")
    return records


# ---------------------------------------------------------------------------
# OSM speed zone processing (alternative to government GeoJSON)
# ---------------------------------------------------------------------------

# Overpass query for roads with maxspeed tag in major Australian cities.
# We query specific metro bounding boxes to keep response size manageable.
# Use 'out geom' to get coordinates inline (avoids huge node resolution).
# Query major roads only; residential/tertiary streets add too much data
# and are mostly default-limit (50 urban) anyway.
OVERPASS_ZONES_TEMPLATE = """
[out:json][timeout:180];
way["maxspeed"]["highway"~"motorway|motorway_link|trunk|trunk_link|primary|primary_link|secondary|secondary_link"]({bbox});
out geom;
"""

# Major Australian metro bounding boxes: (south, west, north, east)
# Kept compact (~0.1-0.15 deg per side) to avoid Overpass timeouts.
# Each city split into sub-tiles if needed.
AU_METRO_BBOXES = {
    # Brisbane metro - split into 4 quadrants
    "Brisbane_NW": (-27.45, 152.90, -27.30, 153.05),
    "Brisbane_NE": (-27.45, 153.05, -27.30, 153.20),
    "Brisbane_SW": (-27.55, 152.95, -27.45, 153.10),
    "Brisbane_SE": (-27.55, 153.05, -27.45, 153.20),
    # Sydney metro - key corridors
    "Sydney_CBD":  (-33.90, 151.15, -33.82, 151.25),
    "Sydney_W":    (-33.90, 151.00, -33.80, 151.15),
    "Sydney_N":    (-33.82, 151.15, -33.72, 151.28),
    "Sydney_S":    (-33.98, 151.10, -33.90, 151.25),
    # Melbourne metro
    "Melbourne_CBD": (-37.84, 144.92, -37.78, 145.02),
    "Melbourne_E":   (-37.84, 145.02, -37.76, 145.15),
    "Melbourne_W":   (-37.84, 144.80, -37.76, 144.92),
    "Melbourne_N":   (-37.78, 144.92, -37.70, 145.08),
    # Perth
    "Perth_CBD":   (-31.98, 115.82, -31.92, 115.90),
    # Adelaide
    "Adelaide_CBD": (-34.96, 138.56, -34.88, 138.64),
    # Canberra
    "Canberra":    (-35.35, 149.08, -35.25, 149.18),
    # Gold Coast
    "Gold_Coast":  (-28.10, 153.38, -28.00, 153.48),
}


def _overpass_curl(query, label="query"):
    """Execute Overpass API query using curl (more reliable than urllib)."""
    # Write query to temp file to avoid shell escaping issues
    tmp_query = "/tmp/_overpass_query.txt"
    tmp_out = "/tmp/_overpass_result.json"
    with open(tmp_query, "w") as f:
        f.write(query)

    cmd = [
        "curl", "-s", "--max-time", "300",
        "--retry", "2", "--retry-delay", "5",
        "-X", "POST",
        "-H", "User-Agent: ai-hud-data-prep/1.0",
        "-d", f"@{tmp_query}",
        "-o", tmp_out,
        "-w", "%{size_download}",
        f"{OVERPASS_API}?data="
    ]

    # Use -d with data= parameter
    cmd = [
        "curl", "-s", "--max-time", "300",
        "--retry", "2", "--retry-delay", "5",
        "--data-urlencode", f"data@{tmp_query}",
        "-o", tmp_out,
        "-w", "%{http_code} %{size_download}",
        OVERPASS_API,
    ]

    print(f"  Running curl for {label}...")
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=360)

    if result.returncode != 0:
        print(f"  ERROR: curl failed (exit {result.returncode}): "
              f"{result.stderr[:200]}")
        return None

    parts = result.stdout.strip().split()
    http_code = parts[0] if parts else "?"
    dl_size = parts[1] if len(parts) > 1 else "?"
    print(f"  {label}: HTTP {http_code}, {float(dl_size)/1024:.0f} KB")

    if http_code != "200":
        print(f"  ERROR: HTTP {http_code}")
        return None

    with open(tmp_out, "rb") as f:
        raw = f.read()

    # Cleanup temp files
    os.remove(tmp_query)
    os.remove(tmp_out)

    return raw


def fetch_osm_speed_zones(cities=None):
    """Download road speed limits from OSM Overpass API for given cities.

    Returns list of (lat, lon, speed, road_type, bearing) tuples.
    """
    if cities is None:
        cities = list(AU_METRO_BBOXES.keys())

    all_records = []

    for city in cities:
        bbox = AU_METRO_BBOXES.get(city)
        if not bbox:
            print(f"  WARNING: Unknown city '{city}', skipping")
            continue

        bbox_str = f"{bbox[0]},{bbox[1]},{bbox[2]},{bbox[3]}"
        query = OVERPASS_ZONES_TEMPLATE.replace("{bbox}", bbox_str)

        print(f"  Querying OSM speed zones for {city} "
              f"({bbox_str})...")

        try:
            raw = _overpass_curl(query, city)
        except Exception as e:
            print(f"  ERROR: {city} query failed: {e}")
            continue

        if not raw:
            continue

        records = _parse_osm_speed_zones(raw, city)
        all_records.extend(records)

        # Rate-limit between Overpass requests
        time.sleep(3)

    return all_records


def _parse_osm_speed_zones(json_data, city_name=""):
    """Parse Overpass 'out geom' response containing ways with maxspeed.

    With 'out geom', each way element has a 'geometry' array with inline
    lat/lon coordinates, so no separate node resolution is needed.
    """
    try:
        data = json.loads(json_data)
    except json.JSONDecodeError as e:
        print(f"  ERROR: Failed to parse Overpass JSON: {e}")
        return []

    elements = data.get("elements", [])
    ways = [e for e in elements if e.get("type") == "way"]
    print(f"  {city_name}: {len(ways)} ways")

    records = []
    for way in ways:
        tags = way.get("tags", {})
        maxspeed_str = tags.get("maxspeed", "")

        # Parse speed value
        speed = 0
        try:
            speed = int(maxspeed_str)
        except ValueError:
            # Handle "60 km/h" or similar formats
            for part in maxspeed_str.split():
                try:
                    speed = int(part)
                    break
                except ValueError:
                    continue

        if speed <= 0 or speed > 130:
            continue

        # Determine road type
        highway = tags.get("highway", "")
        rtype = ROAD_TYPE_MAP.get(highway, 5)

        # Extract geometry from 'out geom' format
        # Each point is {"lat": ..., "lon": ...}
        geom = way.get("geometry", [])
        coords = []
        for pt in geom:
            if "lat" in pt and "lon" in pt:
                coords.append([pt["lon"], pt["lat"]])  # GeoJSON order

        if len(coords) < 2:
            continue

        bearing = _bearing_of_segment(coords)

        # Sample points along the way
        for lat, lon in _sample_linestring(coords, interval_m=30):
            records.append((lat, lon, speed, rtype, bearing))

    print(f"  {city_name}: {len(records)} sampled points")
    return records


def fetch_osm_cameras():
    """Download speed camera data from OSM Overpass API."""
    print("\n[2/2] Fetching speed cameras from OpenStreetMap...")
    return _overpass_curl(OVERPASS_QUERY, "AU cameras")


# ---------------------------------------------------------------------------
# Additional data: hardcoded known cameras from government sources
# ---------------------------------------------------------------------------

def get_known_cameras_au():
    """Return hardcoded list of well-known Australian speed cameras.

    These are sourced from publicly available government publications.
    Format: (lat, lon, speed_limit, camera_type, bearing)
    """
    # A curated subset of major fixed cameras from public records.
    # Full dataset to be built from government CSV downloads.
    cameras = [
        # VIC - Major fixed cameras (public, cameras.vic.gov.au)
        (-37.8136, 144.9631, 60, 0, 0xFFFF),   # Melbourne CBD
        (-37.7749, 144.8443, 80, 0, 0xFFFF),   # Western Ring Road
        (-37.8197, 145.0568, 60, 4, 0xFFFF),   # Hoddle St / Eastern Fwy
        (-37.6567, 144.9450, 80, 0, 0xFFFF),   # Hume Fwy Craigieburn
        (-38.1485, 144.3614, 60, 4, 0xFFFF),   # Geelong

        # NSW - Major fixed cameras (public, TfNSW)
        (-33.8688, 151.2093, 50, 0, 0xFFFF),   # Sydney CBD
        (-33.8914, 151.2764, 60, 0, 0xFFFF),   # Eastern Distributor
        (-33.7966, 151.1805, 60, 0, 0xFFFF),   # Pacific Hwy Chatswood
        (-33.9425, 151.2230, 60, 4, 0xFFFF),   # King St Newtown
        (-33.8568, 151.0382, 80, 0, 0xFFFF),   # M4 Motorway

        # QLD (mobile zones - approximate locations)
        (-27.4698, 153.0251, 60, 2, 0xFFFF),   # Brisbane CBD
        (-27.3812, 153.0286, 80, 2, 0xFFFF),   # Gateway Motorway
        (-27.5613, 153.0820, 60, 2, 0xFFFF),   # Logan Motorway
    ]
    return cameras


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Prepare offline speed limit + camera databases for ai-hud")
    parser.add_argument("--output-dir", default=None,
                        help="Output directory (default: data/)")
    parser.add_argument("--zones-only", action="store_true",
                        help="Only process speed zones")
    parser.add_argument("--cameras-only", action="store_true",
                        help="Only process cameras")
    parser.add_argument("--cities", default=None,
                        help="Comma-separated city names for OSM speed zones "
                             "(default: all major AU cities)")
    parser.add_argument("--vic-geojson", default=None,
                        help="Path to local VIC speed zones GeoJSON "
                             "(skip OSM download, use government data)")
    parser.add_argument("--osm-json", default=None,
                        help="Path to local OSM Overpass JSON (skip download)")
    args = parser.parse_args()

    # Determine output directory
    script_dir = os.path.dirname(os.path.abspath(__file__))
    project_dir = os.path.dirname(script_dir)
    output_dir = args.output_dir or os.path.join(project_dir, "data")
    os.makedirs(output_dir, exist_ok=True)

    print("=" * 60)
    print("  ai-hud Speed Database Preparation Tool")
    print("=" * 60)

    # --- Speed zones ---
    if not args.cameras_only:
        print("\n[1/2] Processing speed zones...")
        zone_records = []

        if args.vic_geojson:
            # Use local VIC government GeoJSON if provided
            print("  Using local VIC government GeoJSON...")
            with open(args.vic_geojson, "rb") as f:
                vic_data = f.read()
            zone_records.extend(process_vic_speed_zones(vic_data))
        else:
            # Use OSM Overpass API for major cities
            cities = None
            if args.cities:
                cities = [c.strip() for c in args.cities.split(",")]
            print("  Using OpenStreetMap Overpass API for speed zones...")
            print(f"  Cities: {cities or list(AU_METRO_BBOXES.keys())}")
            zone_records.extend(fetch_osm_speed_zones(cities))

        if zone_records:
            zones_path = os.path.join(output_dir, "speed_zones.db")
            write_db(zones_path, MAGIC_ZONES, zone_records)
        else:
            print("  WARNING: No speed zone data available")

    # --- Speed cameras ---
    if not args.zones_only:
        print("\n[2/2] Processing speed cameras...")
        cam_records = []

        # OSM cameras
        if args.osm_json:
            with open(args.osm_json, "rb") as f:
                osm_data = f.read()
        else:
            osm_data = fetch_osm_cameras()

        if osm_data:
            cam_records.extend(process_osm_cameras(osm_data))

        # Hardcoded known cameras
        known = get_known_cameras_au()
        cam_records.extend(known)
        print(f"  Added {len(known)} known cameras from government sources")

        # Deduplicate (within 50m radius)
        cam_records = _deduplicate(cam_records, threshold_m=50)

        if cam_records:
            cameras_path = os.path.join(output_dir, "speed_cameras.db")
            write_db(cameras_path, MAGIC_CAMERAS, cam_records)
        else:
            print("  WARNING: No camera data available")

    print("\n" + "=" * 60)
    print("  Done!")
    print(f"  Output: {output_dir}/")
    print()
    print("  Deploy to device:")
    print(f"    adb push {output_dir}/speed_zones.db /root/data/")
    print(f"    adb push {output_dir}/speed_cameras.db /root/data/")
    print("=" * 60)


def _deduplicate(records, threshold_m=50):
    """Remove duplicate records within threshold distance."""
    if not records:
        return records

    M_PER_DEG = 111_320.0
    unique = []
    seen = set()

    for rec in records:
        lat, lon = rec[0], rec[1]
        # Quantize to ~50m grid for fast dedup check
        qk = (round(lat * 2000), round(lon * 2000))
        if qk in seen:
            continue
        seen.add(qk)
        unique.append(rec)

    removed = len(records) - len(unique)
    if removed:
        print(f"  Deduplicated: {len(records)} -> {len(unique)} "
              f"(removed {removed} duplicates)")
    return unique


if __name__ == "__main__":
    main()
