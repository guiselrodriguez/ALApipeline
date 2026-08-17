#!/usr/bin/env python3
"""
HBRL house data pipeline

three tables:

  sensors       Combines a house's raw instrument files (Atmocube, AirAssure,
                Geocene, Hobo, Anemometer, Kestrel, Aranet) into ONE table
                per house, standardized to:
                    house_id, phase, date, timestamp_pst, instrument, variable, value, unit, qc_flag
                'phase' (pre/post) is determined from House_charac's Visit
                 Pre ends 30 min
                before Visit 2, post starts 1 hour after Visit 2 (the visit
                itself takes ~1 hour and is excluded as a disruption buffer).

  house-charac  Builds a static per-house metadata table straight from the
                House_charac spreadsheet (one row per house, not a timeseries).

  ogawa         Builds the Ogawa NO2 badge table (placement + raw +
                background-subtracted + difference values, one row per
                single measurement).

Usage:
    python3 process_hbrl_data.py sensors <house_charac.xlsx> <week1_folder> <week2_folder> [--output <folder>]
    python3 process_hbrl_data.py house-charac <house_charac.xlsx> [output_folder] [--house H1 H2 ...]
    python3 process_hbrl_data.py ogawa <values.xlsx> <placement.xlsx> [output_folder] [--house H1 ...]

Every house/week file is auto-detected from filenames of the form
h<N>_w<M>_<instrument>... Pass every week folder you have for a house to
the `sensors` command in one call -- it merges them into a single H<N>.csv,
automatically deduplicating any reading that shows up in more than one
week's folder (several raw exports contain the FULL multi-week recording
in every week's folder, not just that week's slice -- confirmed for
AirAssure, Atmocube, Geocene, and Kestrel).

"""

import argparse
import csv
import glob
import json
import os
import re
import sys
import shutil
import tempfile
import zipfile
from collections import defaultdict
from datetime import datetime, timedelta, timezone

try:
    import openpyxl
except ImportError:
    openpyxl = None  # only needed for the house-charac and ogawa subcommands

PST = timezone(timedelta(hours=-8))  # fixed UTC-8 

# Full code list per https://tsi.com/resources/airassure-iaq-monitors-faqs
# (the design doc's "common codes" list only covers the first 7 general codes,
# plus it separately calls out 1024/fan_blocked in a worked example).

GENERAL_SENSOR_STATUS_BITS = {
    1: "not_factory_calibrated",
    2: "hardware_fault",
    4: "communication_error",
    8: "data_corrupt",
    16: "data_not_available",
    32: "over_detectable_limit",
    64: "under_detectable_limit",
}

# Bit 256 means something different depending on which sensor's status column
# it's set in (PM vs VOC), so each component status column gets its own combined map.
PM_STATUS_BITS = {
    **GENERAL_SENSOR_STATUS_BITS,
    256: "fan_rpm_error",
    512: "laser_error",
    1024: "fan_blocked",
    2048: "cleaning_cycle_completed",
}

VOC_STATUS_BITS = {
    **GENERAL_SENSOR_STATUS_BITS,
    256: "sensor_unsupported",
}

SYSTEM_STATUS_BITS = {
    1: "device_rebooted",
    2: "not_written_to_sd_card",
    4: "cloud_disconnected",
    8: "battery_low",
    16: "time_not_synced_24h",
    32: "time_invalid",
    64: "eeprom_failure",
}

# status column name -> bitmap to use when decoding it
STATUS_COL_BITMAP = defaultdict(lambda: GENERAL_SENSOR_STATUS_BITS, {
    "PM Status": PM_STATUS_BITS,
    "VOC Status": VOC_STATUS_BITS,
})


def decode_bits(value, bitmap):
    if not value:
        return []
    flags = [name for bit, name in bitmap.items() if value & bit]
    covered = 0
    for bit in bitmap:
        if value & bit:
            covered |= bit
    leftover = value & ~covered
    if leftover:
        # bits the PDF didn't document (it only lists a "common codes" subset)
        flags.append(f"unrecognized_status_bit_{leftover}")
    return flags


def fmt_time(dt):
    return dt.strftime("%I:%M %p").lstrip("0")


# Discovery: group raw files by (house, week)


FNAME_RE = re.compile(r"^[Hh](\d+)_[Ww](\d+)_(.+)\.(csv|txt|json)$")


def discover_groups(folder):
    groups = defaultdict(dict)
    # recursive so it doesn't matter how deep the actual files are nested --
    # Drive downloads often wrap them in an extra subfolder or two
    for path in sorted(glob.glob(os.path.join(folder, "**", "*"), recursive=True)):
        if os.path.isdir(path):
            continue
        base = os.path.basename(path)
        m = FNAME_RE.match(base)
        if not m:
            continue
        house_num, week_num, rest, _ext = m.groups()
        key = (house_num, week_num)
        rest_l = rest.lower()
        if rest_l.startswith("airassure"):
            groups[key].setdefault("airassure", []).append(path)
        elif rest_l.startswith("geocene_left"):
            groups[key]["geocene_left"] = path
        elif rest_l.startswith("geocene_right"):
            groups[key]["geocene_right"] = path
        elif rest_l.startswith("atmocube"):
            groups[key]["atmocube"] = path
        elif rest_l.startswith("kestrel"):
            groups[key]["kestrel"] = path
        elif rest_l.startswith("aranet"):
            groups[key]["aranet"] = path
        elif rest_l.startswith("anemometer"):
            groups[key]["anemometer"] = path
        elif rest_l.startswith("hobo"):
            groups[key]["hobo"] = path
        elif rest_l.startswith("ogawa"):
            groups[key]["ogawa"] = path
        elif rest_l.startswith("house_charac") or rest_l.startswith("housecharac"):
            groups[key]["house_charac"] = path
    return groups


def resolve_input_path(path, temp_dirs):
    """
    Accepts either a folder or a .zip file. If it's a zip, extracts it to a
    temporary directory (so the zip itself never has to be manually
    unzipped/kept around) and returns that temp path instead. Every temp
    dir created gets appended to temp_dirs, so the caller can clean them up
    afterward with cleanup_temp_dirs() -- extraction is temporary, not a
    second permanent copy sitting next to the zip.
    """
    if os.path.isfile(path) and path.lower().endswith(".zip"):
        temp_dir = tempfile.mkdtemp(prefix="hbrl_")
        print(f"Extracting {path} -> {temp_dir} (temporary, removed after processing)")
        with zipfile.ZipFile(path) as zf:
            zf.extractall(temp_dir)
        temp_dirs.append(temp_dir)
        return temp_dir
    return path


def cleanup_temp_dirs(temp_dirs):
    for d in temp_dirs:
        shutil.rmtree(d, ignore_errors=True)


def pt_number(path):
    m = re.search(r"pt(\d+)", os.path.basename(path), re.IGNORECASE)
    return int(m.group(1)) if m else 999


# Anemometer parsing (hood_airflow readings) + gap detection, shared helpers


def parse_anemometer_rows(path):
    rows = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            parts = [p.strip() for p in line.strip().split(",")]
            if len(parts) < 5:
                continue
            date_s, time_s = parts[-2], parts[-1]
            if not re.match(r"^\d{2}-\d{2}-\d{4}$", date_s):
                continue
            try:
                dt_local = datetime.strptime(f"{date_s} {time_s}", "%d-%m-%Y %H:%M:%S")
                value = float(parts[1])
            except ValueError:
                continue
            dt = dt_local.replace(tzinfo=PST).astimezone(timezone.utc).replace(tzinfo=None)
            rows.append((dt, value))
    return rows

def detect_gaps(label, house_num, week_num, timestamps, min_gap_minutes=15):
    ts_sorted = sorted(set(timestamps))
    if len(ts_sorted) < 2:
        return
    diffs_sec = sorted((b - a).total_seconds() for a, b in zip(ts_sorted, ts_sorted[1:]))
    median = diffs_sec[len(diffs_sec) // 2]
    threshold = max(median * 3, min_gap_minutes * 60)
    for a, b in zip(ts_sorted, ts_sorted[1:]):
        gap = (b - a).total_seconds()
        if gap > threshold:
            a_pst, b_pst = a - timedelta(hours=8), b - timedelta(hours=8)
            date_str = a_pst.strftime("%m/%d") if a_pst.date() == b_pst.date() else f"{a_pst.strftime('%m/%d')}-{b_pst.strftime('%m/%d')}"
            print(
                f"House {house_num} Week {week_num} {label}: no data on {date_str} "
                f"between {fmt_time(a_pst)} and {fmt_time(b_pst)} PST"
            )


# Per-instrument parsers. Each returns a list of (dt_pst_naive, variable, value, unit, qc_flag)


ATMOCUBE_MAP = {
    "voc": ("voc", "ppm"),
    "pm1": ("pm1", "ug/m3"),
    "pm25": ("pm25", "ug/m3"),
    "pm4": ("pm4", "ug/m3"),
    "pm10": ("pm10", "ug/m3"),
    "co2": ("co2", "ppm"),
    "t": ("temperature", "C"),
    "h": ("humidity", "%"),
    "abs_h": ("absolute_humidity", "g/m3"),
    "p": ("pressure", "hPa"),
    "noise": ("noise", "dB"),
    "light": ("light", "lux"),
    "ch2o": ("ch2o", "ppb"),
    "voc_index": ("voc_index", "index"),
    "nox_index": ("nox_index", "index"),
}


def parse_atmocube(path):
    rows = []
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            ts = r.get("ts")
            if not ts:
                continue
            try:
                epoch = int(ts)
            except ValueError:
                continue
            dt_utc = datetime.fromtimestamp(epoch, tz=timezone.utc).replace(tzinfo=None)
            for col, (var, unit) in ATMOCUBE_MAP.items():
                val = r.get(col)
                if val in (None, ""):
                    continue
                rows.append((dt_utc, var, val, unit, "ok"))
    return rows


def parse_flexible_datetime(raw):
    raw = raw.strip()
    for fmt in ("%Y-%m-%d %I:%M:%S %p", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


def parse_kestrel(path):
    rows = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        cols = reader.fieldnames or []
        ts_col = next((c for c in cols if "timestamp" in c.lower()), None)
        temp_c_col = next((c for c in cols if "temperature" in c.lower() and "(c)" in c.lower()), None)
        temp_f_col = next((c for c in cols if "temperature" in c.lower() and "(f)" in c.lower()), None)
        rh_col = next((c for c in cols if "humidity" in c.lower()), None)
        for r in reader:
            raw_ts = r.get(ts_col) if ts_col else None
            if not raw_ts:
                continue
            dt_local = parse_flexible_datetime(raw_ts)
            if dt_local is None:
                continue
            dt = dt_local.replace(tzinfo=PST).astimezone(timezone.utc).replace(tzinfo=None)
            if temp_c_col and r.get(temp_c_col) not in (None, ""):
                rows.append((dt, "temperature", r[temp_c_col], "C", "ok"))
            elif temp_f_col and r.get(temp_f_col) not in (None, ""):
                c_val = round((float(r[temp_f_col]) - 32) * 5 / 9, 3)
                rows.append((dt, "temperature", c_val, "C", "ok"))
            if rh_col and r.get(rh_col) not in (None, ""):
                rows.append((dt, "humidity", r[rh_col], "%", "ok"))
    return rows


def parse_aranet(path):
    rows = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        cols = reader.fieldnames or []
        time_col = next((c for c in cols if c.lower().startswith("time")), None)
        co2_col = next((c for c in cols if "carbon dioxide" in c.lower()), None)
        temp_col = next((c for c in cols if "temperature" in c.lower()), None)
        rh_col = next((c for c in cols if "humidity" in c.lower()), None)
        pres_col = next((c for c in cols if "pressure" in c.lower()), None)
        is_fahrenheit = bool(temp_col) and ("f)" in temp_col.lower())
        for r in reader:
            raw_ts = r.get(time_col) if time_col else None
            if not raw_ts:
                continue
            try:
                 dt_local = datetime.strptime(raw_ts.strip(), "%d/%m/%Y %I:%M:%S %p")
            except ValueError:
                continue
            dt = dt_local.replace(tzinfo=PST).astimezone(timezone.utc).replace(tzinfo=None)
            if co2_col and r.get(co2_col) not in (None, ""):
                rows.append((dt, "co2", r[co2_col], "ppm", "ok"))
            if temp_col and r.get(temp_col) not in (None, ""):
                t_val = float(r[temp_col])
                if is_fahrenheit:
                    t_val = round((t_val - 32) * 5 / 9, 3)
                rows.append((dt, "temperature", t_val, "C", "ok"))
            if rh_col and r.get(rh_col) not in (None, ""):
                rows.append((dt, "humidity", r[rh_col], "%", "ok"))
            if pres_col and r.get(pres_col) not in (None, ""):
                rows.append((dt, "pressure", r[pres_col], "hPa", "ok"))
    return rows


def parse_geocene(path, side):
    rows = []
    var = f"stove_temp_{side}"
    with open(path) as f:
        data = json.load(f)
    for entry in data:
        raw_ts = entry.get("timestamp")
        val = entry.get("value")
        if not raw_ts or val is None:
            continue
        dt_utc = datetime.strptime(raw_ts[:19], "%Y-%m-%dT%H:%M:%S")
        rows.append((dt_utc, var, val, "C", "ok"))
    return rows


def parse_hobo(path):
    rows = []
    hobo_tz = timezone(timedelta(hours=-7))  # header states "GMT-07:00"
    with open(path, encoding="utf-8-sig", newline="") as f:
        lines = f.readlines()
    header_idx = next((i for i, l in enumerate(lines[:10]) if "Date Time" in l), 1)
    header = next(csv.reader([lines[header_idx]]))
    dt_idx = next(i for i, h in enumerate(header) if "Date Time" in h)
    pow_idx = next(i for i, h in enumerate(header) if "Active Power" in h)
    for line in lines[header_idx + 1:]:
        if not line.strip():
            continue
        parts = next(csv.reader([line]))
        if len(parts) <= max(dt_idx, pow_idx):
            continue
        raw_ts, raw_pow = parts[dt_idx], parts[pow_idx]
        if not raw_ts or not raw_pow:
            continue
        try:
            dt_local = datetime.strptime(raw_ts, "%m/%d/%y %I:%M:%S %p")
        except ValueError:
            continue
        dt_utc = dt_local.replace(tzinfo=hobo_tz).astimezone(timezone.utc).replace(tzinfo=None)
        rows.append((dt_utc, "power", raw_pow, "W", "ok"))
    return rows


AIRASSURE_VAR_MAP = {
    "Temperature": "temperature",
    "Relative Humidity": "humidity",
    "Barometric Pressure": "pressure",
    "PM1": "pm1", "PM2.5": "pm25", "PM4": "pm4", "PM10": "pm10",
    "CO2": "co2", "CO": "co", "NO2": "no2", "O3": "o3", "SO2": "so2",
    "EtOH": "etoh", "tVOC": "tvoc",
}
AIRASSURE_STATUS_FOR = {
    "Temperature": "Temperature/Relative Humidity Status",
    "Relative Humidity": "Temperature/Relative Humidity Status",
    "Barometric Pressure": "Barometric Pressure Status",
    "PM1": "PM Status", "PM2.5": "PM Status", "PM4": "PM Status", "PM10": "PM Status",
    "CO2": "CO2 Status", "CO": "CO Status", "NO2": "NO2 Status", "O3": "O3 Status", "SO2": "SO2 Status",
    "EtOH": "VOC Status", "tVOC": "VOC Status",
}


def parse_airassure(paths, house_num, week_num):
    header = None
    units = {}
    data_lines = []
    for p in sorted(paths, key=pt_number):
        with open(p, newline="") as f:
            for raw in f:
                line = raw.rstrip("\n")
                if not line.strip() or line.startswith("#"):
                    continue
                if line.startswith("Timestamp,System Status"):
                    if header is None:
                        header = next(csv.reader([line]))
                    continue
                if line.startswith("UTC,"):
                    if not units and header:
                        units = dict(zip(header, next(csv.reader([line]))))
                    continue
                data_lines.append(line)

    if header is None:
        print(f"House {house_num} Week {week_num} AirAssure: no recognizable header found, skipping")
        return []

    rows = []
    seen_ts = set()
    all_ts = []
    ts_errors = defaultdict(list)

    for parts in csv.reader(data_lines):
        rec = dict(zip(header, parts))
        raw_ts = rec.get("Timestamp")
        if not raw_ts:
            continue
        try:
            dt_utc = datetime.strptime(raw_ts, "%m/%d/%Y %H:%M")
        except ValueError:
            continue
        if dt_utc in seen_ts:
            continue
        seen_ts.add(dt_utc)
        all_ts.append(dt_utc)

        try:
            sys_status = int(rec.get("System Status") or 0)
        except ValueError:
            sys_status = 0
        sys_flags = decode_bits(sys_status, SYSTEM_STATUS_BITS)
        # code 4 (cloud disconnected) is expected -- device has no wifi -- so it's not an error on its own
        sys_flags_reportable = [f for f in sys_flags if f != "cloud_disconnected"]
        if sys_flags_reportable:
            ts_errors[dt_utc].append("system: " + "+".join(sys_flags_reportable))

        for col, var in AIRASSURE_VAR_MAP.items():
            val = rec.get(col)
            if val in (None, ""):
                continue
            status_col = AIRASSURE_STATUS_FOR[col]
            try:
                status_val = int(rec.get(status_col) or 0)
            except ValueError:
                status_val = 0
            flags = decode_bits(status_val, STATUS_COL_BITMAP[status_col])
            if status_val != 0 and f"{var}: " + "+".join(flags) not in ts_errors[dt_utc]:
                ts_errors[dt_utc].append(f"{var}: " + "+".join(flags))
            qc_parts = flags + sys_flags_reportable
            qc = "+".join(qc_parts) if qc_parts else "ok"
            unit = units.get(col, "")
            rows.append((dt_utc, var, val, unit, qc))

    for dt_utc in sorted(ts_errors):
        reasons = "; ".join(ts_errors[dt_utc])
        dt_pst_display = dt_utc - timedelta(hours=8)
        print(
            f"House {house_num} Week {week_num} AirAssure at {fmt_time(dt_pst_display)} on "
            f"{dt_pst_display.strftime('%Y-%m-%d')} PST had an error: {reasons}"
        )
    detect_gaps("AirAssure", house_num, week_num, all_ts)
    return rows


# House characteristics table ( per-house metadata, not a timeseries)


HOUSE_CHARAC_COLUMN_MAP = {
    "House": "house_id",
    "Visit 1": "visit_1_date",
    "Visit 2": "visit_2_date",
    "Visit 3": "visit_3_date",
    "Gas Stove Make": "gas_stove_make",
    "Gas Stove Model": "gas_stove_model",
    "Oven Type": "oven_type",
    "Gas Stove Approx. Age (years)": "gas_stove_age_years",
    "Gas Stove Number of Burners": "gas_stove_burners",
    "Range Hood Type": "range_hood_type",
    "Range Hood Make": "range_hood_make",
    "Range Hood Model": "range_hood_model",
    "Range Hood Age (years)": "range_hood_age_years",
    "Range Hood Velocity - Left - Off (m/s)": "hood_velocity_left_off",
    "Range Hood Velocity - Left - High (m/s)": "hood_velocity_left_high",
    "Range Hood Velocity - Left - Low (m/s)": "hood_velocity_left_low",
    "Range Hood Velocity - Right - Off (m/s)": "hood_velocity_right_off",
    "Range Hood Velocity - Right - High (m/s)": "hood_velocity_right_high",
    "Range Hood Velocity - Right - Low (m/s)": "hood_velocity_right_low",
    "Type of House": "house_type",
    "Kitchen Area (sq-ft)": "kitchen_area_sqft",
    "Kitchen Height (ft)": "kitchen_height_ft",
    "House Area (sq-ft)": "house_area_sqft",
    "Number of Floors": "num_floors",
    "Number of Bedrooms": "num_bedrooms",
    "Members of Household": "num_household_members",
    "Other Sources of NO2": "other_no2_sources",
}


def build_house_characteristics(source_file, output_folder, house_filter=None):
    if openpyxl is None:
        print("openpyxl is required for this -- install it with: pip install openpyxl")
        sys.exit(1)

    wb = openpyxl.load_workbook(source_file, data_only=True)
    ws = wb["Sheet1"]
    header_row = [cell.value for cell in ws[2]]

    col_indexes = {}
    for idx, raw_header in enumerate(header_row):
        if raw_header in HOUSE_CHARAC_COLUMN_MAP:
            col_indexes[HOUSE_CHARAC_COLUMN_MAP[raw_header]] = idx
        elif raw_header is not None:
            print(f"Note: unmapped House_charac column found: {raw_header!r} -- skipped")

    rows_out = []
    for row in ws.iter_rows(min_row=3, values_only=True):
        if row[0] is None:
            continue
        record = {}
        for clean_name, idx in col_indexes.items():
            val = row[idx] if idx < len(row) else None
            if clean_name.startswith("visit_") and val is not None:
                val = val.strftime("%Y-%m-%d %H:%M:%S")
            record[clean_name] = val if val is not None else ""
        if house_filter and record.get("house_id") not in house_filter:
            continue
        rows_out.append(record)

    out_path = os.path.join(output_folder, "house_characteristics.csv")
    fieldnames = list(HOUSE_CHARAC_COLUMN_MAP.values())
    with open(out_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows_out)

    print(f"Wrote {len(rows_out)} house(s) to {out_path}")
    return out_path


# Ogawa NO2 table (weekly integrated badge samples, not a timeseries either)

OGAWA_RAW_COL_TO_LOCATION = {
    "Ogawa_1": "kitchen_near_stove",
    "Ogawa_2": "kitchen_far_stove",
    "Ogawa_3": "living_room",
    "Ogawa_4": "bedroom",
    "Ogawa_5_Outside": "outdoors",
    "Ogawa_6_FB": "field_blank",
}
OGAWA_PLACEMENT_COL_TO_LOCATION = {
    "F": "kitchen_near_stove",
    "H": "kitchen_far_stove",
    "J": "living_room",
    "L": "outdoors",
    "N": "bedroom",
}


def _load_ogawa_placement_descriptions(placement_file):
    wb = openpyxl.load_workbook(placement_file, data_only=True)
    ws = wb["Placement"]
    descriptions = {}
    for row in ws.iter_rows(min_row=2, values_only=False):
        house_raw = row[0].value
        if not house_raw:
            continue
        house_id = str(house_raw).strip()
        if house_id in descriptions:
            continue
        row_map = {cell.coordinate[0]: cell.value for cell in row}
        descriptions[house_id] = {
            loc_key: row_map.get(col, "")
            for col, loc_key in OGAWA_PLACEMENT_COL_TO_LOCATION.items()
        }
    return descriptions


def build_ogawa_table(values_file, placement_file, output_folder, house_filter=None):
    if openpyxl is None:
        print("openpyxl is required for this -- install it with: pip install openpyxl")
        sys.exit(1)

    wb = openpyxl.load_workbook(values_file, data_only=True)
    ws = wb["Ogawa_raw"]
    placement = _load_ogawa_placement_descriptions(placement_file)

    header = [cell.value for cell in ws[1]]
    rows_out = []
    skipped = []

    for row in ws.iter_rows(min_row=2, values_only=True):
        record = dict(zip(header, row))
        house_raw = record.get("HouseID")
        phase_raw = record.get("Pre_Post")
        if house_raw is None or phase_raw is None:
            continue
        house_id = f"H{int(house_raw)}"
        phase = str(phase_raw).strip().lower()
        if house_filter and house_id not in house_filter:
            continue

        loc_descriptions = placement.get(house_id, {})
        had_any_value = False
        for col, loc_key in OGAWA_RAW_COL_TO_LOCATION.items():
            val = record.get(col)
            if val is None or val == "":
                continue
            had_any_value = True
            rows_out.append({
                "house_id": house_id,
                "phase": phase,
                "location": loc_key,
                "location_description": loc_descriptions.get(loc_key, ""),
                "value_ppb": val,
            })
        if not had_any_value:
            skipped.append((house_id, phase))

    out_path = os.path.join(output_folder, "ogawa.csv")
    fieldnames = ["house_id", "phase", "location", "location_description", "value_ppb"]
    with open(out_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows_out)

    print(f"Wrote {len(rows_out)} rows to {out_path}")
    print(f"Houses found: {sorted(set(r['house_id'] for r in rows_out))}")
    if skipped:
        print(f"Skipped {len(skipped)} house/phase combo(s) with no results yet: {skipped}")
    return out_path
# Driver


def load_visit_dates(house_charac_file):
    """house_id (e.g. 'H1') -> [visit1_dt, visit2_dt, visit3_dt or None]"""
    wb = openpyxl.load_workbook(house_charac_file, data_only=True)
    ws = wb["Sheet1"]
    header_row = [cell.value for cell in ws[2]]
    idx = {name: i for i, name in enumerate(header_row) if name in ("House", "Visit 1", "Visit 2", "Visit 3")}
    def to_utc(v):
        return v.replace(tzinfo=PST).astimezone(timezone.utc).replace(tzinfo=None) if v else None

    visits = {}
    for row in ws.iter_rows(min_row=3, values_only=True):
        house_id = row[idx["House"]] if "House" in idx else None
        if not house_id:
            continue
        v1 = to_utc(row[idx["Visit 1"]] if "Visit 1" in idx else None)
        v2 = to_utc(row[idx["Visit 2"]] if "Visit 2" in idx else None)
        v3 = to_utc(row[idx["Visit 3"]] if "Visit 3" in idx else None)
        visits[str(house_id).strip()] = (v1, v2, v3)
    return visits


def compute_phase_windows(v1, v2, v3, pre_buffer=timedelta(minutes=30), post_buffer=timedelta(hours=1)):
    """
    Per the team's guidance: the visit itself (~1 hour) disrupts the room,
    so pre ends 30 min BEFORE Visit 2 and post starts 1 hour AFTER Visit 2.
    Everything in between (the visit + its buffer) is excluded entirely.
    """
    pre_window = (v1, v2 - pre_buffer) if v1 and v2 else None
    post_window = (v2 + post_buffer, v3) if v2 and v3 else None
    return pre_window, post_window


def tag_phase(dt, pre_window, post_window):
    if pre_window and pre_window[0] <= dt <= pre_window[1]:
        return "pre"
    if post_window and post_window[0] <= dt <= post_window[1]:
        return "post"
    return None  # falls in the visit buffer itself, or outside the deployment entirely


def process_house(house_num, week_groups, house_charac_file, output_folder):
    """
    week_groups: {week_num: files_dict} -- typically weeks 1 and 2 for this house,
    gathered from however many input folders were given on the command line.
    Produces ONE combined CSV for the house, tagged pre/post using House_charac's
    visit dates instead of the Anemometer-derived window.
    """
    house_id = f"H{house_num}"
    print(f"\n=== Processing House {house_num} (all weeks found: {sorted(week_groups)}) ===")

    visits = load_visit_dates(house_charac_file)
    if house_id not in visits:
        print(f"House {house_num}: not found in {house_charac_file} -- skipping, no visit dates to work from")
        return None
    v1, v2, v3 = visits[house_id]
    if v1 is None or v2 is None or v3 is None:
        print(f"House {house_num}: missing one or more visit dates (v1={v1}, v2={v2}, v3={v3}) -- can't compute pre/post, skipping")
        return None

    pre_window, post_window = compute_phase_windows(v1, v2, v3)
    print("  PRE/POST WINDOWS (from House_charac Visit 1/2/3, converted to true UTC):")
    print(f"    pre:  {pre_window[0]} UTC  ->  {pre_window[1]} UTC")
    print(f"    post: {post_window[0]} UTC  ->  {post_window[1]} UTC")
    print(f"    (visit + buffer excluded: {pre_window[1]} -> {post_window[0]} UTC)")
    print("-" * 42)

    out_rows = []
    counts = defaultdict(lambda: defaultdict(int))  # instrument -> phase/dropped -> count

    def add(instrument, parsed):
        for dt, var, val, unit, qc in parsed:
            phase = tag_phase(dt, pre_window, post_window)
            if phase is None:
                counts[instrument]["dropped_buffer_or_outside"] += 1
                continue
            counts[instrument][phase] += 1
            out_rows.append((dt, phase, instrument, var, val, unit, qc))

    for week_num, files in sorted(week_groups.items()):
        if "atmocube" in files:
            add("Atmocube", parse_atmocube(files["atmocube"]))
        if "kestrel" in files:
            add("Kestrel", parse_kestrel(files["kestrel"]))
        if "aranet" in files:
            add("Aranet", parse_aranet(files["aranet"]))
        if "geocene_left" in files:
            add("Geocene", parse_geocene(files["geocene_left"], "left"))
        if "geocene_right" in files:
            add("Geocene", parse_geocene(files["geocene_right"], "right"))
        if "hobo" in files:
            add("Hobo", parse_hobo(files["hobo"]))
        if "anemometer" in files:
            anem_rows = parse_anemometer_rows(files["anemometer"])
            add("Anemometer", [(dt, "hood_airflow", val, "m/s", "ok") for dt, val in anem_rows])
        if "airassure" in files:
            # No window/trimming argument anymore -- phase tagging + dropping
            # happens uniformly via add() below, same as every other instrument
            airassure_rows = parse_airassure(files["airassure"], house_num, week_num)
            add("AirAssure", airassure_rows)
        if "ogawa" in files:
            print(f"House {house_num} week {week_num}: Ogawa file present here -- use the 'ogawa' subcommand instead, not included in this table")
        if "house_charac" in files:
            print(f"House {house_num} week {week_num}: House_charac file present here -- already used above for visit dates, not included as timeseries rows")

    print("Rows per instrument (pre / post / dropped as visit-buffer or outside deployment):")
    for instrument, c in sorted(counts.items()):
        print(f"  {instrument}: pre={c['pre']}  post={c['post']}  dropped={c['dropped_buffer_or_outside']}")

    seen = set()
    deduped_rows = []
    dupes_removed = defaultdict(int)
    for dt, phase, instrument, var, val, unit, qc in out_rows:
        key = (dt, instrument, var)
        if key in seen:
            dupes_removed[instrument] += 1
            continue
        seen.add(key)
        deduped_rows.append((dt, phase, instrument, var, val, unit, qc))

    if dupes_removed:
        print("Duplicate readings removed (same timestamp+instrument+variable found in more than one week's folder):")
        for instrument, n in sorted(dupes_removed.items()):
            print(f"  {instrument}: {n} duplicate(s) removed")

    out_rows = deduped_rows
    out_rows.sort(key=lambda r: (r[0], r[2], r[3]))

    out_name = f"{house_id}.csv"
    out_path = os.path.join(output_folder, out_name)
    fields = ["house_id", "phase", "date", "timestamp_utc", "instrument", "variable", "value", "unit", "qc_flag"]
    with open(out_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(fields)
        for dt, phase, instrument, var, val, unit, qc in out_rows:
            writer.writerow([house_id, phase, dt.strftime("%Y-%m-%d"), dt.strftime("%Y-%m-%d %H:%M:%S"), instrument, var, val, unit, qc])

    print(f"Wrote {len(out_rows)} rows to {out_path}")
    return out_path


DEFAULT_OUTPUT_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")


def parse_cli_datetime(raw):
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


def build_arg_parser():
    parser = argparse.ArgumentParser(
        prog="process_hbrl_data.py",
        description="Build the HBRL standardized tables: sensor data, house characteristics, or Ogawa NO2.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    sensors_p = subparsers.add_parser(
        "sensors",
        help="Process a house's raw instrument folders into ONE combined table, tagged pre/post using House_charac visit dates",
    )
    sensors_p.add_argument("house_charac_file", help="Path to the House Characteristics .xlsx file (used for pre/post windows, not Anemometer)")
    sensors_p.add_argument("input_folders", nargs="+", help="One or more week folders (or .zip files) for the SAME house, e.g. the week1 folder/zip and the week2 folder/zip")
    sensors_p.add_argument(
        "--output", default=None,
        help='Where to write H<N>.csv (default: an "output" folder next to this script)',
    )

    house_p = subparsers.add_parser(
        "house-charac",
        help="Build the house characteristics table from the House_charac spreadsheet",
    )
    house_p.add_argument("source_file", help="Path to the House Characteristics .xlsx file")
    house_p.add_argument(
        "output_folder", nargs="?", default=None,
        help='Where to write house_characteristics.csv (default: an "output" folder next to this script)',
    )
    house_p.add_argument(
        "--house", nargs="*", default=None,
        help="Only include specific house(s), e.g. --house H1 H2 (default: include every house found)",
    )

    ogawa_p = subparsers.add_parser(
        "ogawa",
        help="Build the Ogawa NO2 table from the values + placement spreadsheets",
    )
    ogawa_p.add_argument("values_file", help="Path to the Ogawa NO2 values .xlsx file")
    ogawa_p.add_argument("placement_file", help="Path to the Ogawa placement .xlsx file")
    ogawa_p.add_argument(
        "output_folder", nargs="?", default=None,
        help='Where to write ogawa.csv (default: an "output" folder next to this script)',
    )
    ogawa_p.add_argument(
        "--house", nargs="*", default=None,
        help="Only include specific house(s), e.g. --house H1 (default: include every house found)",
    )

    return parser


def main():
    args = build_arg_parser().parse_args()

    if args.command == "sensors":
        temp_dirs = []
        try:
            all_groups = defaultdict(dict)  # (house_num, week_num) -> files, merged across all input folders
            for folder in args.input_folders:
                resolved = resolve_input_path(folder, temp_dirs)
                groups = discover_groups(resolved)
                for key, files in groups.items():
                    all_groups[key].update(files)
            if not all_groups:
                print(f"No h<N>_w<M>_... files found in: {', '.join(args.input_folders)}")
                sys.exit(1)

            # Re-group by house only, since output is now one combined file per house
            by_house = defaultdict(dict)  # house_num -> {week_num: files}
            for (house_num, week_num), files in all_groups.items():
                by_house[house_num][week_num] = files

            output_folder = args.output or DEFAULT_OUTPUT_FOLDER
            os.makedirs(output_folder, exist_ok=True)
            print(f"Writing output to {output_folder}")
            for house_num in sorted(by_house):
                process_house(house_num, by_house[house_num], args.house_charac_file, output_folder)
        finally:
            cleanup_temp_dirs(temp_dirs)

    elif args.command == "house-charac":
        output_folder = args.output_folder or DEFAULT_OUTPUT_FOLDER
        os.makedirs(output_folder, exist_ok=True)
        print(f"Writing output to {output_folder}")
        build_house_characteristics(args.source_file, output_folder, house_filter=args.house)

    elif args.command == "ogawa":
        output_folder = args.output_folder or DEFAULT_OUTPUT_FOLDER
        os.makedirs(output_folder, exist_ok=True)
        print(f"Writing output to {output_folder}")
        build_ogawa_table(args.values_file, args.placement_file, output_folder, house_filter=args.house)


if __name__ == "__main__":
    main()