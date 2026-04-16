#!/usr/bin/env python3
"""
Build a "perfect weather days per year" dataset using NOAA GSOD data
from AWS S3 (public bucket, no auth required).

Pipeline:
  1. Download ISD station history from S3  →  filter global stations
  2. Pick one representative station per 3-degree geographic cell
  3. Fetch GSOD CSVs (2021, 2022, 2023) for each chosen station
  4. Count "perfect" days, average over the 3 years
  5. Write weather_data.js for index.html

Perfect-weather criteria (NOAA GSOD native units):
  MAX temperature : 64.4–80.6 °F  (18–27 °C)
  MIN temperature : > 50.0 °F      (> 10 °C)
  Precipitation   : < 0.079 in/day  (< 2 mm)
  Max wind speed  : < 16.2 knots   (< 30 km/h)
  All four fields must be valid (non-missing) to score a day.

Data sources (all public, no API key):
  ISD history : https://noaa-isd-pds.s3.amazonaws.com/isd-history.csv
  GSOD daily  : https://noaa-gsod-pds.s3.amazonaws.com/{YEAR}/{USAFWBAN}.csv
"""

import csv
import io
import json
import math
import os
import time
import requests

# ── Region ────────────────────────────────────────────────────────────────────
LAT_MIN, LAT_MAX = -60.0, 83.0   # Antarctica edge → Arctic (skip deep Antarctic)
LON_MIN, LON_MAX = -180.0, 180.0  # Full global coverage
CELL_DEG = 3                      # degrees per selection cell

# ── Perfect-day thresholds (GSOD native units) ────────────────────────────────
T_MAX_LO  = 64.4    # °F   (18 °C)
T_MAX_HI  = 80.6    # °F   (27 °C)
T_MIN_LO  = 50.0    # °F   (10 °C)
PRCP_HI   = 0.0787  # in   ( 2 mm)
MXSPD_HI  = 16.20   # kts  (30 km/h)

MISSING_T    = 9999.9
MISSING_PRCP = 99.99
MISSING_WIND = 999.9
MIN_VALID_DAYS_PER_YEAR = 100   # skip station-year if fewer valid obs
YEARS = [2021, 2022, 2023]

# ── S3 endpoints ──────────────────────────────────────────────────────────────
ISD_URL  = "https://noaa-isd-pds.s3.amazonaws.com/isd-history.csv"
GSOD_S3  = "https://noaa-gsod-pds.s3.amazonaws.com"

# ── Files ─────────────────────────────────────────────────────────────────────
CHECKPOINT = "checkpoint_gsod.json"
OUTPUT_JS  = "weather_data.js"

DELAY = 0.12   # seconds between S3 GETs (well within S3 limits)


# ── Helpers ───────────────────────────────────────────────────────────────────

def cell_key(lat, lon):
    """Return (cell_lat, cell_lon) bottom-left corner of CELL_DEG-degree cell."""
    return (math.floor(lat / CELL_DEG) * CELL_DEG,
            math.floor(lon / CELL_DEG) * CELL_DEG)


def load_isd_stations():
    """Download ISD history and return list of Americas station dicts."""
    print("Downloading ISD station history …")
    r = requests.get(ISD_URL, timeout=60)
    r.raise_for_status()

    stations = []
    reader = csv.DictReader(io.StringIO(r.text))
    for row in reader:
        try:
            lat_s = row['LAT'].strip()
            lon_s = row['LON'].strip()
            if not lat_s or not lon_s:
                continue
            lat = float(lat_s)
            lon = float(lon_s)
            # Bounding box
            if not (LAT_MIN <= lat <= LAT_MAX and LON_MIN <= lon <= LON_MAX):
                continue
            # Skip 0,0 (invalid placeholder)
            if lat == 0.0 and lon == 0.0:
                continue
            # Require active through at least 2021
            end = row['END'].strip()
            if end and end.isdigit() and int(end) < 20210101:
                continue

            usaf = row['USAF'].strip().zfill(6)
            wban = row['WBAN'].strip().zfill(5)
            stations.append({
                'id':   usaf + wban,
                'lat':  lat,
                'lon':  lon,
                'name': row['STATION NAME'].strip(),
            })
        except (ValueError, KeyError):
            continue

    print(f"  {len(stations)} global stations found in ISD history")
    return stations


def select_one_per_cell(stations):
    """For each CELL_DEG-degree cell, keep the station nearest the cell centre."""
    cells = {}
    for st in stations:
        ck = cell_key(st['lat'], st['lon'])
        centre_lat = ck[0] + CELL_DEG / 2
        centre_lon = ck[1] + CELL_DEG / 2
        dist2 = (st['lat'] - centre_lat) ** 2 + (st['lon'] - centre_lon) ** 2
        if ck not in cells or dist2 < cells[ck]['dist2']:
            cells[ck] = {**st, 'dist2': dist2}
    selected = list(cells.values())
    print(f"  {len(selected)} representative stations selected "
          f"(one per {CELL_DEG}° cell)")
    return selected


def fetch_gsod_year(station_id, year):
    """Download one station-year CSV from S3. Returns raw text or None."""
    url = f"{GSOD_S3}/{year}/{station_id}.csv"
    try:
        r = requests.get(url, timeout=45)
        if r.status_code == 200:
            return r.text
    except Exception:
        pass
    return None


def count_perfect_days(csv_text):
    """Parse GSOD CSV text; return (valid_days, perfect_days)."""
    valid = 0
    perfect = 0
    reader = csv.DictReader(io.StringIO(csv_text))
    for row in reader:
        try:
            mx   = float(row['MAX'].strip())
            mn   = float(row['MIN'].strip())
            prcp = float(row['PRCP'].strip())
            wspd = float(row['MXSPD'].strip())
        except (ValueError, KeyError):
            continue

        if mx >= MISSING_T or mn >= MISSING_T:
            continue
        if prcp >= MISSING_PRCP or wspd >= MISSING_WIND:
            continue

        valid += 1
        if (T_MAX_LO <= mx <= T_MAX_HI and
                mn   >= T_MIN_LO and
                prcp < PRCP_HI  and
                wspd < MXSPD_HI):
            perfect += 1

    return valid, perfect


def main():
    # ── 1. Station inventory ──────────────────────────────────────────────────
    all_stations  = load_isd_stations()
    selected      = select_one_per_cell(all_stations)
    total         = len(selected)
    print(f"  Estimated fetch time: ~{total * len(YEARS) * DELAY / 60:.1f} min\n")

    # ── 2. Load checkpoint ────────────────────────────────────────────────────
    done: dict = {}
    if os.path.exists(CHECKPOINT):
        with open(CHECKPOINT) as f:
            done = json.load(f)
        print(f"Resumed from checkpoint "
              f"({sum(1 for v in done.values() if v is not None)} valid, "
              f"{sum(1 for v in done.values() if v is None)} no-data)\n")

    results = []

    # ── 3. Fetch & score ──────────────────────────────────────────────────────
    for i, st in enumerate(selected):
        sid = st['id']

        if sid in done:
            results.append({'lat': st['lat'], 'lon': st['lon'],
                            'perfect': done[sid]})
            continue

        yearly_perfect = []
        for year in YEARS:
            text = fetch_gsod_year(sid, year)
            if text is None:
                time.sleep(DELAY)
                continue
            valid_days, perfect_days = count_perfect_days(text)
            if valid_days >= MIN_VALID_DAYS_PER_YEAR:
                yearly_perfect.append(perfect_days)
            time.sleep(DELAY)

        avg = round(sum(yearly_perfect) / len(yearly_perfect), 1) \
              if yearly_perfect else None

        done[sid] = avg
        results.append({'lat': st['lat'], 'lon': st['lon'], 'perfect': avg})

        # Save checkpoint & print progress every 25 stations
        if (i + 1) % 25 == 0 or i == total - 1:
            with open(CHECKPOINT, 'w') as f:
                json.dump(done, f)
            v = sum(1 for r in results if r['perfect'] is not None)
            n = sum(1 for r in results if r['perfect'] is None)
            pct = (i + 1) / total * 100
            print(f"  [{pct:5.1f}%] {i+1}/{total} stations  |  "
                  f"{v} valid, {n} no-data")

    # ── 4. Write output ───────────────────────────────────────────────────────
    output = [[round(r['lat'], 4), round(r['lon'], 4), r['perfect']]
              for r in results]

    valid   = sum(1 for _, _, v in output if v is not None)
    no_data = total - valid
    print(f"\nComplete: {valid} valid stations, {no_data} no-data")

    header = (
        "// Weather data: [lat, lon, avg_perfect_days_per_year]  (null = no data)\n"
        "// Source: NOAA GSOD via AWS S3, stations from ISD-history, 2021-2023\n"
        "// Perfect day: max 18-27°C, min >10°C, precip <2mm, max wind <30km/h\n"
    )
    body = f"var WEATHER_DATA = {json.dumps(output, separators=(',', ':'))};\n"

    with open(OUTPUT_JS, 'w') as f:
        f.write(header + body)

    print(f"Written  : {OUTPUT_JS}  ({valid} data points)")
    print("\nNext step:")
    print("  python3 -m http.server 8080   →   open http://localhost:8080")


if __name__ == '__main__':
    main()
