"""
PM2.5 indoor / outdoor (I/O) ratio and indoor-minus-outdoor difference, per house.

Indoor PM2.5 comes from the H<N>.csv files that process_hbrl_data.py writes
(AirAssure and Atmocube, each compared separately). Outdoor PM2.5 comes from
the PurpleAir download for the house's closest station
(files named H<N>_purpleair_<station>.csv).

How a day's number is made (nothing is pooled across days or houses):
  1. Average indoor and outdoor readings into clock hours (PDT).
  2. Keep only hours where both indoor and outdoor have data, so a partial
     outdoor day is never compared to a full indoor day.
  3. Daily indoor and daily outdoor value = mean of those shared hours.
  4. Ratio = indoor / outdoor.  Difference = indoor - outdoor.
  5. A house's pre / post value = median across its days.

Outdoor readings are cleaned before step 1: when both PurpleAir channels (A and
B) are available, a reading is dropped if they differ by more than
--max-channel-difference (ug/m3). Use --channel-a-only for a house whose
channel B is bad.

The ratio plots stop at --ratio-max (default 10) so the boxes are easier to
see. Days above that are not drawn; each one is counted in a small "\u25b2n"
label at the top of its box, and the CSVs still hold every value. Use
--ratio-max 0 to show everything.

Outputs, in --output-dir:
  io_daily_values.csv     one row per house / phase / instrument / day
  io_house_summary.csv    median across days, one row per house / phase / instrument
  io_ratio_airassure.png, io_ratio_atmocube.png
  io_difference_airassure.png, io_difference_atmocube.png

Usage:
    python make_io_plots.py output --purpleair-dir purpleair --output-dir io_plots
    python make_io_plots.py output --purpleair-dir purpleair --channel-a-only H7
    python make_io_plots.py output --purpleair-dir purpleair --min-hours 12
    python make_io_plots.py output --purpleair-dir purpleair --ratio-max 5
    python make_io_plots.py output\\H8.csv --purpleair-dir purpleair
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
INSTRUMENTS = ["AirAssure", "Atmocube"]
PHASE_COLORS = {"pre": "#9ecae1", "post": "#3182bd"}


def house_sort_key(house_id):
    return int(re.sub(r"\D", "", house_id))


def find_indoor_files(inputs):
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


def find_purpleair_file(purpleair_dir, house_id):
    matches = sorted(glob.glob(os.path.join(purpleair_dir, f"{house_id}_purpleair_*.csv")))
    return matches[0] if matches else None


def load_indoor(path):
    readings = pd.read_csv(path, usecols=["house_id", "phase", "timestamp_utc", "instrument", "variable", "value"],
                           parse_dates=["timestamp_utc"])
    readings = readings[(readings["variable"] == "pm25") & (readings["instrument"].isin(INSTRUMENTS))].copy()
    readings["value"] = pd.to_numeric(readings["value"], errors="coerce")
    readings = readings.dropna(subset=["value"])
    readings["local_hour"] = (readings["timestamp_utc"] - PDT_OFFSET).dt.floor("h")
    return readings


def load_outdoor(path, channel_a_only, max_channel_difference):
    """PurpleAir PM2.5 (ATM). Returns the cleaned readings and how many were dropped."""
    outdoor = pd.read_csv(path)
    outdoor["timestamp_utc"] = pd.to_datetime(outdoor["time_stamp"]).dt.tz_localize(None)
    dropped = 0
    if channel_a_only:
        outdoor["value"] = outdoor["pm2.5_atm_a"]
    else:
        outdoor["value"] = outdoor["pm2.5_atm"]
        both_channels = outdoor["pm2.5_atm_a"].notna() & outdoor["pm2.5_atm_b"].notna()
        disagree = both_channels & ((outdoor["pm2.5_atm_a"] - outdoor["pm2.5_atm_b"]).abs() > max_channel_difference)
        dropped = int(disagree.sum())
        outdoor = outdoor[~disagree]
    outdoor = outdoor.dropna(subset=["value"]).copy()
    outdoor["local_hour"] = (outdoor["timestamp_utc"] - PDT_OFFSET).dt.floor("h")
    return outdoor, dropped


def daily_io_values(house_id, indoor, outdoor, min_hours):
    hourly_outdoor = outdoor.groupby("local_hour")["value"].mean().rename("outdoor_value").reset_index()
    tables = []
    for instrument in INSTRUMENTS:
        instrument_readings = indoor[indoor["instrument"] == instrument]
        if instrument_readings.empty:
            continue
        hourly_indoor = instrument_readings.groupby(["phase", "local_hour"])["value"].mean().rename("indoor_value").reset_index()
        shared_hours = hourly_indoor.merge(hourly_outdoor, on="local_hour")  # only hours with both
        if shared_hours.empty:
            continue
        shared_hours["day"] = shared_hours["local_hour"].dt.date
        daily = shared_hours.groupby(["phase", "day"]).agg(
            indoor_mean=("indoor_value", "mean"),
            outdoor_mean=("outdoor_value", "mean"),
            hours_shared=("local_hour", "nunique"),
        ).reset_index()
        daily = daily[daily["hours_shared"] >= min_hours].copy()
        daily["ratio"] = np.where(daily["outdoor_mean"] > 0, daily["indoor_mean"] / daily["outdoor_mean"], np.nan)
        daily["difference"] = daily["indoor_mean"] - daily["outdoor_mean"]
        daily.insert(0, "instrument", instrument)
        daily.insert(0, "house_id", house_id)
        tables.append(daily)
    return pd.concat(tables, ignore_index=True) if tables else pd.DataFrame()


def make_boxplot(daily_values, instrument, column, title, y_label, reference_line, output_path, y_max=None):
    subset = daily_values[daily_values["instrument"] == instrument]
    if subset.empty:
        print(f"No data for {instrument} -- skipped {os.path.basename(output_path)}")
        return
    houses = sorted(subset["house_id"].unique(), key=house_sort_key)
    figure, axes = plt.subplots(figsize=(max(7, 1.1 * len(houses) + 2), 5))
    random_generator = np.random.default_rng(0)

    for position, house_id in enumerate(houses):
        for offset, phase in ((-0.2, "pre"), (0.2, "post")):
            values = subset[(subset["house_id"] == house_id) & (subset["phase"] == phase)][column].dropna()
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

    axes.axhline(reference_line, color="gray", linestyle="--", linewidth=1, zorder=0)
    axes.set_xticks(range(len(houses)))
    axes.set_xticklabels(houses)
    axes.set_xlim(-0.6, len(houses) - 0.4)
    axes.set_ylabel(y_label)
    axes.set_title(title)
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
    parser = argparse.ArgumentParser(description="PM2.5 I/O ratio and indoor-minus-outdoor difference, per house")
    parser.add_argument("inputs", nargs="+", help="A folder holding the H<N>.csv files, or the H<N>.csv files themselves")
    parser.add_argument("--purpleair-dir", required=True, help="Folder holding the H<N>_purpleair_<station>.csv files")
    parser.add_argument("--output-dir", default="io_plots", help="Folder to save the tables and figures into")
    parser.add_argument("--min-hours", type=int, default=0, help="Leave out days with fewer than this many shared indoor/outdoor hours (default 0 keeps every day)")
    parser.add_argument("--max-channel-difference", type=float, default=5.0, help="Drop an outdoor reading when PurpleAir channels A and B differ by more than this many ug/m3 (default 5)")
    parser.add_argument("--ratio-max", type=float, default=10.0, help="Top of the y-axis on the ratio plots (default 10; days above it are marked, not drawn). 0 shows everything")
    parser.add_argument("--channel-a-only", nargs="*", default=[], help="Houses whose outdoor data should use channel A only, e.g. --channel-a-only H7")
    return parser


def main():
    args = build_arg_parser().parse_args()
    os.makedirs(args.output_dir, exist_ok=True)
    channel_a_houses = {house.upper() for house in args.channel_a_only}

    indoor_paths = find_indoor_files(args.inputs)
    if not indoor_paths:
        print("No H<N>.csv files found")
        return

    tables = []
    for path in indoor_paths:
        house_id = os.path.splitext(os.path.basename(path))[0].upper()
        purpleair_path = find_purpleair_file(args.purpleair_dir, house_id)
        if purpleair_path is None:
            print(f"{house_id}: no PurpleAir file found in {args.purpleair_dir} -- skipped")
            continue
        outdoor, dropped = load_outdoor(purpleair_path, house_id in channel_a_houses, args.max_channel_difference)
        if outdoor.empty:
            print(f"{house_id}: PurpleAir file {os.path.basename(purpleair_path)} has no usable readings -- skipped")
            continue
        indoor = load_indoor(path)
        daily = daily_io_values(house_id, indoor, outdoor, args.min_hours)
        if daily.empty:
            print(f"{house_id}: no hours where indoor and outdoor both have data -- skipped")
            continue
        note = "channel A only" if house_id in channel_a_houses else f"{dropped} outdoor reading(s) dropped for A/B disagreement"
        print(f"{house_id}: {os.path.basename(purpleair_path)} ({note}); {daily['day'].nunique()} days")
        tables.append(daily)

    if not tables:
        print("Nothing to plot")
        return
    daily_values = pd.concat(tables, ignore_index=True)

    daily_path = os.path.join(args.output_dir, "io_daily_values.csv")
    daily_values.to_csv(daily_path, index=False)
    print(f"Saved {daily_path}")

    summary = daily_values.groupby(["house_id", "phase", "instrument"]).agg(
        median_indoor=("indoor_mean", "median"),
        median_outdoor=("outdoor_mean", "median"),
        median_ratio=("ratio", "median"),
        median_difference=("difference", "median"),
        days=("day", "nunique"),
    ).reset_index()
    summary["house_number"] = summary["house_id"].map(house_sort_key)
    summary = summary.sort_values(["instrument", "house_number", "phase"], ascending=[True, True, False]).drop(columns="house_number")
    summary_path = os.path.join(args.output_dir, "io_house_summary.csv")
    summary.to_csv(summary_path, index=False)
    print(f"Saved {summary_path}")

    for instrument in INSTRUMENTS:
        name = instrument.lower()
        make_boxplot(daily_values, instrument, "ratio",
                     f"{instrument} PM2.5 indoor / outdoor ratio -- daily, by house, pre vs post",
                     "Indoor / outdoor PM2.5 (ratio)", 1.0,
                     os.path.join(args.output_dir, f"io_ratio_{name}.png"), y_max=args.ratio_max or None)
        make_boxplot(daily_values, instrument, "difference",
                     f"{instrument} PM2.5 indoor minus outdoor -- daily, by house, pre vs post",
                     "Indoor minus outdoor PM2.5 (\u00b5g/m\u00b3)", 0.0,
                     os.path.join(args.output_dir, f"io_difference_{name}.png"))


if __name__ == "__main__":
    main()