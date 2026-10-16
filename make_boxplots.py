"""
Pre vs post box plots, one figure per pollutant, one pair of boxes per house.

Reads the H<N>.csv files that process_hbrl_data.py's `sensors` command writes.
Each house gets two boxes side by side (pre, then post). The points behind a box
are that house's daily values for that phase, so the line inside the box is the
median across days.

Days are calendar days in PDT (local time). The first and last day of each
phase are usually partial days. 

Variables:
  AirAssure: PM1, PM2.5, PM10, NO2, CO2
  Atmocube:  PM1, PM2.5, PM10, NOx index, CO2
 
The math summarized:
Daily value is the sum of the day's readings divided by how many there are.
A house's pre or post number is the median of its daily means. That's the line inside the box.
Change is post minus pre. For skewed pollutants like PM it's post ÷ pre.
NO2 and NOx: use the difference only, because many houses have a pre median of 0 and you can't divide by 0.

How to read a box
The line is the typical day.
The box covers the middle half of the days (25th to 75th percentile).
The whiskers reach the farthest normal day, within 1.5 times the box height of the box edge.
Dots past the whiskers are unusual days. The grey dots are individual days.


Usage:
    python make_boxplots.py output --output-dir boxplots
    python make_boxplots.py output --min-hours 12
    python make_boxplots.py output\\H5.csv output\\H8.csv
"""

import argparse
import glob
import os
import re
from datetime import timedelta

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.patches import Patch

PDT_OFFSET = timedelta(hours=7)

# (instrument, variable, label, unit)
METRICS = [
    ("AirAssure", "pm1", "PM1", "\u00b5g/m\u00b3"),
    ("AirAssure", "pm25", "PM2.5", "\u00b5g/m\u00b3"),
    ("AirAssure", "pm10", "PM10", "\u00b5g/m\u00b3"),
    ("AirAssure", "no2", "NO2", "ppb"),
    ("AirAssure", "co2", "CO2", "ppm"),
    ("Atmocube", "pm1", "PM1", "\u00b5g/m\u00b3"),
    ("Atmocube", "pm25", "PM2.5", "\u00b5g/m\u00b3"),
    ("Atmocube", "pm10", "PM10", "\u00b5g/m\u00b3"),
    ("Atmocube", "nox_index", "NOx index", "unitless"),
    ("Atmocube", "co2", "CO2", "ppm"),
]

PHASE_COLORS = {"pre": "#9ecae1", "post": "#3182bd"}
COLUMNS = ["house_id", "phase", "timestamp_utc", "instrument", "variable", "value"]


def daily_values_for_file(path, min_hours):
    """One row per house / phase / instrument / variable / day."""
    readings = pd.read_csv(path, usecols=COLUMNS, parse_dates=["timestamp_utc"])
    metric_table = pd.DataFrame(METRICS, columns=["instrument", "variable", "label", "unit"])
    readings = readings.merge(metric_table[["instrument", "variable"]], on=["instrument", "variable"])
    readings["value"] = pd.to_numeric(readings["value"], errors="coerce")
    readings = readings.dropna(subset=["value"])
    if readings.empty:
        return pd.DataFrame()

    local_time = readings["timestamp_utc"] - PDT_OFFSET  # PDT, so a "day" is a local day
    readings["day"] = local_time.dt.date
    readings["hour"] = local_time.dt.floor("h")

    grouped = readings.groupby(["house_id", "phase", "instrument", "variable", "day"])
    daily = grouped["value"].mean()
    hours_with_data = grouped["hour"].nunique()
    result = pd.DataFrame({"daily_value": daily, "hours_with_data": hours_with_data}).reset_index()
    return result[result["hours_with_data"] >= min_hours]


def house_sort_key(house_id):
    return int(re.sub(r"\D", "", house_id))


def make_boxplot(daily_values, instrument, variable, label, unit, output_dir):
    subset = daily_values[(daily_values["instrument"] == instrument) & (daily_values["variable"] == variable)]
    if subset.empty:
        print(f"No data for {instrument} {label} -- skipped")
        return

    houses = sorted(subset["house_id"].unique(), key=house_sort_key)
    figure, axes = plt.subplots(figsize=(max(7, 1.1 * len(houses) + 2), 5))
    random_generator = np.random.default_rng(0)

    for position, house_id in enumerate(houses):
        for offset, phase in ((-0.2, "pre"), (0.2, "post")):
            values = subset[(subset["house_id"] == house_id) & (subset["phase"] == phase)]["daily_value"]
            if values.empty:
                continue
            box_position = position + offset
            axes.boxplot(
                values, positions=[box_position], widths=0.35, patch_artist=True, showfliers=False,
                boxprops=dict(facecolor=PHASE_COLORS[phase]), medianprops=dict(color="black", linewidth=1.5),
            )
            jitter = random_generator.uniform(-0.06, 0.06, len(values))
            axes.scatter(box_position + jitter, values, s=12, color="black", alpha=0.5, zorder=3)

    axes.set_xticks(range(len(houses)))
    axes.set_xticklabels(houses)
    axes.set_xlim(-0.6, len(houses) - 0.4)
    axes.set_ylabel(f"{label} ({unit})")
    axes.set_title(f"{instrument} {label} -- daily mean by house, pre vs post")
    axes.grid(axis="y", alpha=0.3)
    axes.legend(handles=[Patch(facecolor=PHASE_COLORS["pre"], label="pre"), Patch(facecolor=PHASE_COLORS["post"], label="post")])
    plt.tight_layout()

    output_path = os.path.join(output_dir, f"boxplot_{instrument.lower()}_{variable}.png")
    plt.savefig(output_path, dpi=150)
    plt.close(figure)
    print(f"Saved {output_path}")


def find_house_files(inputs):
    paths = []
    for item in inputs:
        if os.path.isdir(item):
            paths.extend(glob.glob(os.path.join(item, "H*.csv")))
        else:
            paths.append(item)
    return sorted(path for path in paths if re.match(r"^H\d+\.csv$", os.path.basename(path), re.IGNORECASE))


def build_arg_parser():
    parser = argparse.ArgumentParser(description="Pre vs post box plots, one pair of boxes per house")
    parser.add_argument("inputs", nargs="+", help="A folder holding the H<N>.csv files, or the H<N>.csv files themselves")
    parser.add_argument("--output-dir", default="boxplots", help="Folder to save the figures into")
    parser.add_argument("--min-hours", type=int, default=0, help="Leave out days with fewer than this many hours of data (default 0 keeps every day)")
    return parser


def main():
    args = build_arg_parser().parse_args()
    os.makedirs(args.output_dir, exist_ok=True)

    paths = find_house_files(args.inputs)
    if not paths:
        print("No H<N>.csv files found")
        return
    print(f"Reading {len(paths)} house file(s): {', '.join(os.path.basename(path) for path in paths)}")

    tables = [daily_values_for_file(path, args.min_hours) for path in paths]
    tables = [table for table in tables if not table.empty]
    if not tables:
        print("No usable AirAssure or Atmocube data found")
        return
    daily_values = pd.concat(tables, ignore_index=True)

    daily_path = os.path.join(args.output_dir, "daily_values.csv")
    daily_values.to_csv(daily_path, index=False)
    print(f"Saved {daily_path}")

    for instrument, variable, label, unit in METRICS:
        make_boxplot(daily_values, instrument, variable, label, unit, args.output_dir)


if __name__ == "__main__":
    main()