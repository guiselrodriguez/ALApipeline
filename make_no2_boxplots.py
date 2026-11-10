"""
NO2 box plots: how much NO2 was in each house, before vs after the stove.

What it does
  For every house, it looks at the AirAssure NO2 readings (in ppb), works out
  how much NO2 there was on each day, and draws two boxes per house: one for the
  pre days and one for the post days.

How one day becomes one number
  The sensor takes a reading about once a minute. One reading tells you how much
  NO2 there is right then, but not for how long it lasted. So we use time too:
  between two neighboring readings we multiply the average NO2 by the time
  between them, then add that up over the whole day. This is the area under the
  NO2-over-time curve, and its unit is ppb x hours.
  Example: 10 ppb for 30 minutes counts as 10 x 0.5 = 5 ppb x hours.
  For a full day, dividing the result by 24 gives that day's average in ppb.

How to read the plot
  - Each dot is one day for one house.
  - Each box covers the middle half of that house's days, and the black line
    inside is the median day.
  - NO2 is 0 ppb most of the time and only rises when someone cooks, so most
    days are near zero and a few days stick out. That is normal.

Things to know
  - A day with less data recorded gets a smaller number, because there is less
    time to add up. The first and last day of each phase are usually partial.
    The hours_covered column shows how much of each day was recorded, and
    --min-hours leaves out days below a cutoff.
  - If two readings are more than --max-gap-minutes apart, that stretch is left
    out. The script never guesses what happened in a gap.
  - Nothing is pooled. Each day is its own number, and each house is its own
    pair of boxes.

Details, if you need them
  Neighboring readings are combined with the trapezoid rule. The time between
  two readings counts toward the local (PDT) day it starts in, and only when
  both readings are in the same phase.

Outputs, in --output-dir:
  no2_daily_integral.csv   one row per house / phase / day
  no2_house_summary.csv    median across days, one row per house / phase
  no2_integral_boxplot.png

Usage:
    python make_no2_boxplots.py output --output-dir no2_plots
    python make_no2_boxplots.py output --output-dir no2_plots --min-hours 20
    python make_no2_boxplots.py output --output-dir no2_plots --y-max 20
    python make_no2_boxplots.py output\\H5.csv output\\H8.csv
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
PHASE_COLORS = {"pre": "#9ecae1", "post": "#3182bd"}


def house_sort_key(house_id):
    return int(re.sub(r"\D", "", house_id))


def find_house_files(inputs):
    paths = []
    for item in inputs:
        if os.path.isdir(item):
            paths.extend(glob.glob(os.path.join(item, "H*.csv")))
        else:
            paths.append(item)
    return sorted(
        (path for path in paths if re.match(r"^H\d+\.csv$", os.path.basename(path), re.IGNORECASE)),
        key=lambda path: house_sort_key(os.path.basename(path)),
    )


def daily_integral_for_file(path, max_gap_minutes, min_hours):
    readings = pd.read_csv(path, usecols=["house_id", "phase", "timestamp_utc", "instrument", "variable", "value"],
                           parse_dates=["timestamp_utc"])
    readings = readings[(readings["instrument"] == "AirAssure") & (readings["variable"] == "no2")].copy()
    readings["value"] = pd.to_numeric(readings["value"], errors="coerce")
    readings = readings.dropna(subset=["value"]).sort_values("timestamp_utc").reset_index(drop=True)
    if len(readings) < 2:
        return pd.DataFrame()

    next_time = readings["timestamp_utc"].shift(-1)
    next_value = readings["value"].shift(-1)
    next_phase = readings["phase"].shift(-1)
    gap_minutes = (next_time - readings["timestamp_utc"]).dt.total_seconds() / 60
    usable = (gap_minutes > 0) & (gap_minutes <= max_gap_minutes) & (next_phase == readings["phase"])

    hours = gap_minutes[usable] / 60
    area = (readings["value"] + next_value)[usable] / 2 * hours  # ppb x hours
    pieces = pd.DataFrame({
        "house_id": readings.loc[usable, "house_id"],
        "phase": readings.loc[usable, "phase"],
        "day": (readings.loc[usable, "timestamp_utc"] - PDT_OFFSET).dt.date,
        "area": area,
        "hours": hours,
    })
    daily = pieces.groupby(["house_id", "phase", "day"]).agg(
        no2_ppb_hours=("area", "sum"), hours_covered=("hours", "sum")).reset_index()
    return daily[daily["hours_covered"] >= min_hours]


def make_boxplot(daily_values, y_max, output_path):
    houses = sorted(daily_values["house_id"].unique(), key=house_sort_key)
    figure, axes = plt.subplots(figsize=(max(7, 1.1 * len(houses) + 2), 5))
    random_generator = np.random.default_rng(0)

    for position, house_id in enumerate(houses):
        for offset, phase in ((-0.2, "pre"), (0.2, "post")):
            values = daily_values[(daily_values["house_id"] == house_id) & (daily_values["phase"] == phase)]["no2_ppb_hours"]
            if values.empty:
                continue
            box_position = position + offset
            axes.boxplot(
                values, positions=[box_position], widths=0.35, patch_artist=True, showfliers=False,
                boxprops=dict(facecolor=PHASE_COLORS[phase]), medianprops=dict(color="black", linewidth=1.5),
            )
            jitter = random_generator.uniform(-0.06, 0.06, len(values))
            axes.scatter(box_position + jitter, values, s=12, color="black", alpha=0.5, zorder=3)
            days_above = int((values > y_max).sum()) if y_max else 0
            if days_above:
                axes.text(box_position, y_max, f"\u25b2{days_above}", ha="center", va="top", fontsize=9, color="dimgray")

    axes.set_xticks(range(len(houses)))
    axes.set_xticklabels(houses)
    axes.set_xlim(-0.6, len(houses) - 0.4)
    axes.set_ylabel("Daily NO2, area under the curve (ppb\u00b7hours)")
    axes.set_title("AirAssure NO2 -- daily integrated, by house, pre vs post")
    axes.grid(axis="y", alpha=0.3)
    if y_max:
        axes.set_ylim(-0.05 * y_max, y_max)
        axes.annotate(f"Days above {y_max:g} are not shown; \u25b2n marks how many.", xy=(0, -0.12),
                      xycoords="axes fraction", fontsize=8, color="dimgray")
    axes.legend(handles=[Patch(facecolor=PHASE_COLORS["pre"], label="pre"), Patch(facecolor=PHASE_COLORS["post"], label="post")],
                loc="upper left", bbox_to_anchor=(1.01, 1))
    plt.tight_layout()
    plt.savefig(output_path, dpi=150, bbox_inches="tight")
    plt.close(figure)
    print(f"Saved {output_path}")


def build_arg_parser():
    parser = argparse.ArgumentParser(description="Daily integrated AirAssure NO2, pre vs post, per house")
    parser.add_argument("inputs", nargs="+", help="A folder holding the H<N>.csv files, or the H<N>.csv files themselves")
    parser.add_argument("--output-dir", default="no2_plots", help="Folder to save the tables and figure into")
    parser.add_argument("--max-gap-minutes", type=float, default=10.0, help="Leave out the time between two readings that are further apart than this (default 10)")
    parser.add_argument("--min-hours", type=float, default=0.0, help="Leave out days with fewer than this many hours of data (default 0 keeps every day)")
    parser.add_argument("--y-max", type=float, default=0.0, help="Top of the y-axis (default 0 = show everything). Days above it are marked, not drawn")
    return parser


def main():
    args = build_arg_parser().parse_args()
    os.makedirs(args.output_dir, exist_ok=True)

    paths = find_house_files(args.inputs)
    if not paths:
        print("No H<N>.csv files found")
        return

    tables = []
    for path in paths:
        daily = daily_integral_for_file(path, args.max_gap_minutes, args.min_hours)
        house_id = os.path.splitext(os.path.basename(path))[0].upper()
        if daily.empty:
            print(f"{house_id}: no AirAssure NO2 data -- skipped")
            continue
        short_days = int((daily["hours_covered"] < 20).sum())
        print(f"{house_id}: {len(daily)} days ({short_days} with under 20 hours of data)")
        tables.append(daily)
    if not tables:
        print("Nothing to plot")
        return
    daily_values = pd.concat(tables, ignore_index=True)
    daily_values["mean_ppb"] = daily_values["no2_ppb_hours"] / daily_values["hours_covered"]

    daily_path = os.path.join(args.output_dir, "no2_daily_integral.csv")
    daily_values.to_csv(daily_path, index=False)
    print(f"Saved {daily_path}")

    summary = daily_values.groupby(["house_id", "phase"]).agg(
        median_ppb_hours=("no2_ppb_hours", "median"),
        median_mean_ppb=("mean_ppb", "median"),
        days=("day", "nunique"),
    ).reset_index()
    summary["house_number"] = summary["house_id"].map(house_sort_key)
    summary = summary.sort_values(["house_number", "phase"], ascending=[True, False]).drop(columns="house_number")
    summary_path = os.path.join(args.output_dir, "no2_house_summary.csv")
    summary.to_csv(summary_path, index=False)
    print(f"Saved {summary_path}")

    make_boxplot(daily_values, args.y_max or None, os.path.join(args.output_dir, "no2_integral_boxplot.png"))


if __name__ == "__main__":
    main()