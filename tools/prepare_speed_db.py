#!/usr/bin/env python3
"""Prepare offline speed limit + camera databases for ai-hud device.

Configuration-driven: all data sources, cities, and known cameras are
defined in speed_db_config.yaml. Edit that file to add/remove/update
regions and data sources without changing this script.

Output:
  data/speed_zones.db       -- binary speed zone database (AU)
  data/speed_cameras.db     -- binary camera location database (AU)
  data/speed_zones_cn.db    -- binary speed zone database (CN)
  data/speed_cameras_cn.db  -- binary camera location database (CN)

Usage:
  python tools/prepare_speed_db.py                    # AU (default)
  python tools/prepare_speed_db.py --region cn        # CN
  python tools/prepare_speed_db.py --region au --cities Sydney_CBD,Melbourne_CBD
  python tools/prepare_speed_db.py --zones-only
  python tools/prepare_speed_db.py --cameras-only
  python tools/prepare_speed_db.py --output-dir /path/to/output
  python tools/prepare_speed_db.py --list-cities      # show available cities
  python tools/prepare_speed_db.py --config /path/to/config.yaml

Requirements:
  Python 3.8+, PyYAML, internet access for OSM downloads.
"""

import argparse
import json
import math
import os
import struct
import subprocess
import sys
import time

try:
    import yaml
except ImportError:
    print("ERROR: PyYAML is required. Install with: pip install pyyaml")
    sys.exit(1)

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

# Camera type mapping from config string to int
CAM_TYPE_INT = {
    "fixed": 0,
    "red_light": 1,
    "mobile": 2,
    "average": 3,
    "point_to_point": 4,
}

ROAD_TYPE_MAP = {
    "motorway": 0, "motorway_link": 0,
    "trunk": 1, "trunk_link": 1,
    "primary": 2, "primary_link": 2,
    "secondary": 3, "secondary_link": 3,
    "tertiary": 3, "tertiary_link": 3,
    "residential": 4, "living_street": 4,
    "unclassified": 5, "service": 5,
}


# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------

def load_config(config_path=None):
    """Load speed_db_config.yaml from the same directory as this script."""
    if config_path is None:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        config_path = os.path.join(script_dir, "speed_db_config.yaml")

    if not os.path.exists(config_path):
        print(f"ERROR: Config file not found: {config_path}")
        sys.exit(1)

    with open(config_path, "r") as f:
        config = yaml.safe_load(f)

    return config


def get_region_config(config, region_key):
    """Get region-specific configuration."""
    regions = config.get("regions", {})
    if region_key not in regions:
        available = ", ".join(regions.keys())
        print(f"ERROR: Unknown region '{region_key}'. Available: {available}")
        sys.exit(1)
    return regions[region_key]


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

def grid_key(lat, lon, grid_res):
    """Compute grid bucket key (must match speed_db.py)."""
    gi = int(math.floor(lat / grid_res))
    gj = int(math.floor(lon / grid_res))
    gi_u = (gi + 32768) & 0xFFFF
    gj_u = (gj + 32768) & 0xFFFF
    return (gi_u << 16) | gj_u


def write_db(path, magic, records, grid_res):
    """Write binary database file."""
    keyed = []
    for lat, lon, speed, rtype, bearing in records:
        gk = grid_key(lat, lon, grid_res)
        lat_e6 = int(round(lat * 1_000_000))
        lon_e6 = int(round(lon * 1_000_000))
        keyed.append((gk, lat_e6, lon_e6, speed, rtype, bearing))

    keyed.sort(key=lambda x: x[0])

    with open(path, "wb") as f:
        f.write(struct.pack(HEADER_FMT, magic, DB_VERSION,
                            len(keyed), RECORD_SIZE, 0))
        for gk, lat_e6, lon_e6, speed, rtype, bearing in keyed:
            f.write(struct.pack(RECORD_FMT,
                                lat_e6, lon_e6, speed, rtype, bearing, gk))

    size_kb = os.path.getsize(path) / 1024
    print(f"  Wrote {len(keyed):,} records -> {path} ({size_kb:.1f} KB)")


def deduplicate(records, threshold_m=50):
    """Remove duplicate records within threshold distance."""
    if not records:
        return records

    unique = []
    seen = set()

    for rec in records:
        lat, lon = rec[0], rec[1]
        qk = (round(lat * 2000), round(lon * 2000))
        if qk in seen:
            continue
        seen.add(qk)
        unique.append(rec)

    removed = len(records) - len(unique)
    if removed:
        print(f"  Deduplicated: {len(records):,} -> {len(unique):,} "
              f"(removed {removed:,} duplicates)")
    return unique


# ---------------------------------------------------------------------------
# Overpass API
# ---------------------------------------------------------------------------

def overpass_curl(query, label, api_url):
    """Execute Overpass API query using curl."""
    tmp_query = "/tmp/_overpass_query.txt"
    tmp_out = "/tmp/_overpass_result.json"
    with open(tmp_query, "w") as f:
        f.write(query)

    cmd = [
        "curl", "-s", "--max-time", "300",
        "--retry", "2", "--retry-delay", "5",
        "--data-urlencode", f"data@{tmp_query}",
        "-o", tmp_out,
        "-w", "%{http_code} %{size_download}",
        api_url,
    ]

    print(f"  Running curl for {label}...")
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=360)

    if result.returncode != 0:
        print(f"  ERROR: curl failed (exit {result.returncode}): "
              f"{result.stderr[:200]}")
        return None

    parts = result.stdout.strip().split()
    http_code = parts[0] if parts else "?"
    dl_size = parts[1] if len(parts) > 1 else "0"
    print(f"  {label}: HTTP {http_code}, {float(dl_size)/1024:.0f} KB")

    if http_code != "200":
        print(f"  ERROR: HTTP {http_code}")
        return None

    with open(tmp_out, "rb") as f:
        raw = f.read()

    os.remove(tmp_query)
    os.remove(tmp_out)
    return raw


# ---------------------------------------------------------------------------
# Geometry utilities
# ---------------------------------------------------------------------------

def sample_linestring(coords, interval_m=30):
    """Sample points along a LineString at fixed intervals."""
    M_PER_DEG = 111_320.0

    if len(coords) < 2:
        if coords:
            yield (coords[0][1], coords[0][0])
        return

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

    yield (coords[-1][1], coords[-1][0])


def bearing_of_segment(coords):
    """Compute bearing from first to last point."""
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


# ---------------------------------------------------------------------------
# Speed zones processing
# ---------------------------------------------------------------------------

def process_vic_geojson(geojson_data):
    """Parse VIC speed zones GeoJSON."""
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

        rtype = 5  # default: other

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
            bearing = bearing_of_segment(coords)
            for lat, lon in sample_linestring(coords, interval_m=30):
                records.append((lat, lon, speed, rtype, bearing))

    if skipped:
        print(f"  Skipped {skipped} features (no valid speed/geometry)")
    print(f"  Generated {len(records):,} sample points from speed zones")
    return records


def fetch_osm_speed_zones(cities, bbox_dict, config):
    """Download road speed limits from OSM Overpass API.

    Args:
        cities: list of city names to query
        bbox_dict: dict mapping city names to [south, west, north, east]
        config: global config dict
    """
    api_url = config.get("overpass_api")
    delay = config.get("overpass_delay", 3)
    interval = config.get("sample_interval_m", 30)
    road_types = config.get("osm_road_types", [])
    highway_filter = "|".join(road_types)

    query_template = (
        '[out:json][timeout:180];\n'
        f'way["maxspeed"]["highway"~"{highway_filter}"]({{bbox}});\n'
        'out geom;\n'
    )

    all_records = []

    for city in cities:
        bbox = bbox_dict.get(city)
        if not bbox:
            print(f"  WARNING: Unknown city '{city}', skipping")
            continue

        bbox_str = f"{bbox[0]},{bbox[1]},{bbox[2]},{bbox[3]}"
        query = query_template.replace("{bbox}", bbox_str)

        print(f"  Querying OSM speed zones for {city} ({bbox_str})...")

        try:
            raw = overpass_curl(query, city, api_url)
        except Exception as e:
            print(f"  ERROR: {city} query failed: {e}")
            continue

        if not raw:
            continue

        records = parse_osm_speed_zones(raw, city, interval)
        all_records.extend(records)
        time.sleep(delay)

    return all_records


def parse_osm_speed_zones(json_data, city_name, interval_m=30):
    """Parse Overpass 'out geom' response."""
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

        speed = 0
        try:
            speed = int(maxspeed_str)
        except ValueError:
            for part in maxspeed_str.split():
                try:
                    speed = int(part)
                    break
                except ValueError:
                    continue

        if speed <= 0 or speed > 130:
            continue

        highway = tags.get("highway", "")
        rtype = ROAD_TYPE_MAP.get(highway, 5)

        geom = way.get("geometry", [])
        coords = []
        for pt in geom:
            if "lat" in pt and "lon" in pt:
                coords.append([pt["lon"], pt["lat"]])

        if len(coords) < 2:
            continue

        bearing = bearing_of_segment(coords)
        for lat, lon in sample_linestring(coords, interval_m=interval_m):
            records.append((lat, lon, speed, rtype, bearing))

    print(f"  {city_name}: {len(records):,} sampled points")
    return records


# ---------------------------------------------------------------------------
# Camera processing
# ---------------------------------------------------------------------------

def parse_osm_cameras(json_data):
    """Parse OSM Overpass API JSON for speed cameras."""
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

        cam_type = 0
        enforcement = tags.get("enforcement", "")
        if "traffic_signals" in enforcement or "red_light" in enforcement:
            cam_type = 1
        elif "average_speed" in enforcement:
            cam_type = 3

        if tags.get("highway") == "speed_camera":
            cam_type = 0

        speed = 0
        maxspeed = tags.get("maxspeed", "")
        if maxspeed:
            try:
                speed = int(maxspeed)
            except ValueError:
                pass

        bearing = 0xFFFF
        direction = tags.get("direction", "")
        if direction:
            try:
                bearing = int(float(direction)) % 360
            except ValueError:
                pass

        records.append((lat, lon, speed, cam_type, bearing))

    print(f"  Extracted {len(records)} camera locations from OSM")
    return records


def load_known_cameras(region_config):
    """Load known cameras from config YAML."""
    cameras_raw = region_config.get("known_cameras", [])
    records = []
    for cam in cameras_raw:
        lat = cam["lat"]
        lon = cam["lon"]
        speed = cam.get("speed", 0)
        cam_type = CAM_TYPE_INT.get(cam.get("type", "fixed"), 0)
        bearing = 0xFFFF
        records.append((lat, lon, speed, cam_type, bearing))
    return records


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Prepare offline speed limit + camera databases for ai-hud")
    parser.add_argument("--region", default="au",
                        help="Target region key from config (default: au)")
    parser.add_argument("--config", default=None,
                        help="Path to config YAML (default: speed_db_config.yaml)")
    parser.add_argument("--output-dir", default=None,
                        help="Output directory (default: data/)")
    parser.add_argument("--zones-only", action="store_true",
                        help="Only process speed zones")
    parser.add_argument("--cameras-only", action="store_true",
                        help="Only process cameras")
    parser.add_argument("--cities", default=None,
                        help="Comma-separated city names (default: all)")
    parser.add_argument("--vic-geojson", default=None,
                        help="Path to local VIC speed zones GeoJSON (AU only)")
    parser.add_argument("--osm-json", default=None,
                        help="Path to local OSM Overpass JSON (skip download)")
    parser.add_argument("--list-cities", action="store_true",
                        help="List available cities for a region and exit")
    parser.add_argument("--list-regions", action="store_true",
                        help="List available regions and exit")
    args = parser.parse_args()

    config = load_config(args.config)

    # List modes
    if args.list_regions:
        print("Available regions:")
        for key, rc in config.get("regions", {}).items():
            desc = rc.get("description", "")
            n_cities = len(rc.get("cities", {}))
            n_cameras = len(rc.get("known_cameras", []))
            print(f"  {key}: {desc} ({n_cities} cities, {n_cameras} known cameras)")
        return

    region_key = args.region.lower()
    region_config = get_region_config(config, region_key)

    if args.list_cities:
        print(f"Available cities for '{region_key}':")
        for name, bbox in region_config.get("cities", {}).items():
            print(f"  {name}: [{bbox[0]}, {bbox[1]}, {bbox[2]}, {bbox[3]}]")
        return

    # Resolve paths
    script_dir = os.path.dirname(os.path.abspath(__file__))
    project_dir = os.path.dirname(script_dir)
    output_dir = args.output_dir or os.path.join(project_dir, "data")
    os.makedirs(output_dir, exist_ok=True)

    grid_res = config.get("grid_resolution", 0.005)
    suffix = region_config.get("output_suffix", "")
    zones_filename = f"speed_zones{suffix}.db"
    cameras_filename = f"speed_cameras{suffix}.db"
    description = region_config.get("description", region_key.upper())

    print("=" * 60)
    print(f"  ai-hud Speed Database Builder  [{description}]")
    print(f"  Config version: {config.get('version', '?')}")
    print(f"  Last updated:   {config.get('last_updated', '?')}")
    print("=" * 60)

    # --- Speed zones ---
    if not args.cameras_only:
        print("\n[1/2] Processing speed zones...")
        zone_records = []

        if region_key == "au" and args.vic_geojson:
            print("  Using local VIC government GeoJSON...")
            with open(args.vic_geojson, "rb") as f:
                vic_data = f.read()
            zone_records.extend(process_vic_geojson(vic_data))
        else:
            bbox_dict = region_config.get("cities", {})
            cities = None
            if args.cities:
                cities = [c.strip() for c in args.cities.split(",")]
            else:
                cities = list(bbox_dict.keys())

            print(f"  Using OpenStreetMap Overpass API for speed zones...")
            print(f"  Cities ({len(cities)}): {', '.join(cities)}")
            zone_records.extend(
                fetch_osm_speed_zones(cities, bbox_dict, config))

        if zone_records:
            zones_path = os.path.join(output_dir, zones_filename)
            write_db(zones_path, MAGIC_ZONES, zone_records, grid_res)
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
            camera_query = region_config.get("overpass_camera_query", "")
            if camera_query:
                api_url = config.get("overpass_api")
                osm_data = overpass_curl(camera_query,
                                         f"{description} cameras", api_url)
            else:
                osm_data = None

        if osm_data:
            cam_records.extend(parse_osm_cameras(osm_data))

        # Known cameras from config
        known = load_known_cameras(region_config)
        cam_records.extend(known)
        if known:
            print(f"  Added {len(known)} known cameras from config")

        # Deduplicate
        cam_records = deduplicate(cam_records, threshold_m=50)

        if cam_records:
            cameras_path = os.path.join(output_dir, cameras_filename)
            write_db(cameras_path, MAGIC_CAMERAS, cam_records, grid_res)
        else:
            print("  WARNING: No camera data available")

    print("\n" + "=" * 60)
    print("  Done!")
    print(f"  Output: {output_dir}/")
    print()
    print("  Deploy to device:")
    print(f"    adb push {output_dir}/{zones_filename} /root/data/")
    print(f"    adb push {output_dir}/{cameras_filename} /root/data/")
    print("=" * 60)


if __name__ == "__main__":
    main()
