#!/usr/bin/env python3
"""
Build regions.js:
  • US counties   → Natural Earth 10m admin-2 counties (1.7 MB on GitHub)
  • Non-US Americas → Natural Earth 10m admin-1 states/provinces (12 MB on GitHub)
  Each polygon is enriched with the 'perfect_days' value from its nearest
  NOAA weather station (read from weather_data.js).

Run once after fetch_data.py; commit the resulting regions.js.
"""

import json
import math
import os
import re
import requests

# ── Region bounds ─────────────────────────────────────────────────────────────
LAT_MIN, LAT_MAX = 7.0,  84.0    # Panama → high Arctic
LON_MIN, LON_MAX = -140.0, -55.0  # Alaska → Atlantic coast

MAX_DIST_DEG = 5.5   # ignore station if centroid is farther away (deg)
COORD_PREC   = 3     # round coordinate decimals to trim file size

# ── GitHub raw URLs (accessible through this environment's proxy) ─────────────
ADMIN2_URL = (
    "https://raw.githubusercontent.com/nvkelso/natural-earth-vector"
    "/master/geojson/ne_10m_admin_2_counties.geojson"
)
ADMIN1_URL = (
    "https://raw.githubusercontent.com/nvkelso/natural-earth-vector"
    "/master/geojson/ne_10m_admin_1_states_provinces.geojson"
)

OUTPUT = "regions.js"


# ── Helpers ───────────────────────────────────────────────────────────────────

def load_stations():
    with open("weather_data.js") as f:
        text = f.read()
    m = re.search(r"var WEATHER_DATA\s*=\s*(\[.+?\]);", text, re.DOTALL)
    if not m:
        raise ValueError("WEATHER_DATA not found in weather_data.js")
    raw = json.loads(m.group(1))
    return [(r[0], r[1], r[2]) for r in raw if r[2] is not None]


def centroid_of_ring(ring):
    lons = [c[0] for c in ring]
    lats = [c[1] for c in ring]
    return sum(lats) / len(lats), sum(lons) / len(lons)


def geom_centroid(geom):
    t = geom.get("type", "")
    if t == "Polygon":
        return centroid_of_ring(geom["coordinates"][0])
    if t == "MultiPolygon":
        biggest = max(geom["coordinates"], key=lambda p: len(p[0]))
        return centroid_of_ring(biggest[0])
    return None


def nearest_station(clat, clon, stations):
    best_val, best_d = None, float("inf")
    for slat, slon, val in stations:
        d = math.hypot(clat - slat, clon - slon)
        if d < best_d:
            best_d, best_val = d, val
    return best_val, best_d


def round_coords(obj, prec):
    if isinstance(obj, list):
        return [round_coords(v, prec) for v in obj]
    if isinstance(obj, float):
        return round(obj, prec)
    return obj


def download(url, label):
    fname = url.split("/")[-1]
    print(f"  GET {fname} …", end=" ", flush=True)
    try:
        r = requests.get(url, timeout=300)
        r.raise_for_status()
        data = r.json()
        n = len(data.get("features", []))
        print(f"OK  ({len(r.content)//1024} KB, {n} features)")
        return data
    except Exception as exc:
        print(f"FAIL  ({exc})")
        return None


def process(features, stations, label, only_admins=None, skip_admins=None):
    """
    Filter features to bounding box, optionally restrict/exclude countries,
    assign nearest station, return enriched Feature list.
    only_admins : set of admin strings to keep (None = keep all)
    skip_admins : set of admin strings to exclude (None = exclude none)
    """
    out = []
    for feat in features:
        geom  = feat.get("geometry")
        props = feat.get("properties") or {}
        if not geom:
            continue

        # Extract country name — check both lower and UPPER case (admin-1 vs admin-2)
        adm = (props.get("admin") or props.get("ADMIN") or
               props.get("adm0_a3") or props.get("ADM0_A3") or
               props.get("sov_a3")  or props.get("SOV_A3") or
               props.get("gu_a3")   or props.get("GU_A3")  or
               props.get("iso_a2")  or props.get("ISO_A2") or "")

        if only_admins and adm not in only_admins:
            continue
        if skip_admins and adm in skip_admins:
            continue

        c = geom_centroid(geom)
        if not c:
            continue
        clat, clon = c

        if not (LAT_MIN <= clat <= LAT_MAX and LON_MIN <= clon <= LON_MAX):
            continue

        val, d = nearest_station(clat, clon, stations)
        perfect_days = val if d <= MAX_DIST_DEG else None

        name = (props.get("name") or props.get("NAME") or props.get("NAME_EN") or
                props.get("gn_name") or props.get("NAME_ALT") or "")

        out.append({
            "type": "Feature",
            "geometry": {
                "type": geom["type"],
                "coordinates": round_coords(geom["coordinates"], COORD_PREC),
            },
            "properties": {
                "name":         name,
                "admin":        adm,
                "perfect_days": perfect_days,
            },
        })

    valid   = sum(1 for f in out if f["properties"]["perfect_days"] is not None)
    no_data = len(out) - valid
    print(f"    {label}: {len(out)} regions  ({valid} with data, {no_data} no-data)")
    return out


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    stations = load_stations()
    print(f"Loaded {len(stations)} weather stations\n")

    all_features = []

    # ── 1. US counties (admin-2) ──────────────────────────────────────────────
    print("[1/2]  US Counties — Natural Earth 10m admin-2")
    admin2 = download(ADMIN2_URL, "admin-2 counties")
    if admin2:
        # The 'admin' field in admin-2 is the country name
        us_feats = process(
            admin2["features"], stations,
            "US counties",
            only_admins={"United States of America"},
        )
        all_features.extend(us_feats)

    # ── 2. Non-US Americas admin-1 (Canada, Mexico, Central America) ──────────
    print("\n[2/2]  Americas admin-1 — Natural Earth 10m admin-1")
    admin1 = download(ADMIN1_URL, "admin-1 states/provinces")
    if admin1:
        # Exclude the US — we already have county-level US from admin-2
        non_us = process(
            admin1["features"], stations,
            "Americas admin-1 (non-US)",
            skip_admins={"United States of America"},
        )
        all_features.extend(non_us)

    # ── Summary & write ───────────────────────────────────────────────────────
    total = len(all_features)
    valid = sum(1 for f in all_features if f["properties"]["perfect_days"] is not None)
    print(f"\nTotal: {total} regions  ({valid} with data, {total-valid} no-data)")

    if total == 0:
        print("ERROR: no features — regions.js not written")
        return

    geojson_out = {"type": "FeatureCollection", "features": all_features}
    body = json.dumps(geojson_out, separators=(",", ":"))

    header = (
        "// Perfect-weather choropleth data\n"
        "// US counties: Natural Earth 10m admin-2\n"
        "// Americas admin-1: Natural Earth 10m admin-1\n"
        "// properties.perfect_days = avg days/yr (null = no data)\n"
    )
    with open(OUTPUT, "w") as f:
        f.write(header + f"var REGIONS_DATA = {body};\n")

    size_kb = os.path.getsize(OUTPUT) / 1024
    print(f"Written {OUTPUT}  ({size_kb:.0f} KB)")


if __name__ == "__main__":
    main()
