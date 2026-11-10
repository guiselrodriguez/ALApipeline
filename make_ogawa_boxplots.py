"""
Ogawa NO2 box plots: pre vs post, one dot per house.


  Reads the Ogawa lab results (the Ogawa_raw sheet) and makes three plots.
  Each plot has two boxes, pre and post. Each dot is one house, and the line in
  each box is the median house.

The math 
  Badges 1 to 4 are the four rooms, badge 5 is outdoors, badge 6 is the field
  blank (FB), and badge 7 is the lab blank (not used here).
    outdoor            = outdoor - FB
    indoor - outdoor   = room - outdoor - FB             (for each room)
    I/O ratio          = (room - FB) / (outdoor - FB)    (for each room)
  Each house and phase gets ONE number. By default that is the mean of its four
  rooms.


Outputs, in --output-dir:
  ogawa_summary.csv                       one row per house / phase, with every number
  ogawa_outdoor_boxplot.png
  ogawa_indoor_minus_outdoor_boxplot.png
  ogawa_io_ratio_boxplot.png

Usage:
    python make_ogawa_boxplots.py "ALA Aim 2 Ogawa Values.xlsx" --output-dir ogawa_plots
    python make_ogawa_boxplots.py "ALA Aim 2 Ogawa Values.xlsx" --rooms all
"""

import argparse
import os
import re

import matplotlib.pyplot as plt
import numpy as np
import openpyxl
import pandas as pd
from matplotlib.patches import Patch

PHASE_COLORS = {"pre": "#9ecae1", "post": "#3182bd"}
ROOM_COLUMNS = ["Ogawa_1", "Ogawa_2", "Ogawa_3", "Ogawa_4"]


def house_sort_key(house_id):
    return int(re.sub(r"\D", "", house_id))


def clean_note(value):
    return "" if value is None or (isinstance(value, float) and np.isnan(value)) else str(value).strip()


def load_ogawa_values(path):
    worksheet = openpyxl.load_workbook(path, data_only=True)["Ogawa_raw"]
    header = [cell.value for cell in worksheet[1]]
    rows = []
    for row in worksheet.iter_rows(min_row=2, values_only=True):
        record = dict(zip(header, row))
        if record.get("HouseID") in (None, "") or record.get("Pre_Post") in (None, ""):
            continue
        record["house_id"] = f"H{int(record['HouseID'])}"
        record["phase"] = str(record["Pre_Post"]).strip().lower()
        rows.append(record)
    return pd.DataFrame(rows)


def calculate_numbers(values):
    """One row per house / phase / room, then one row per house / phase."""
    per_room = []
    for _, record in values.iterrows():
        outdoor = record["Ogawa_5_Outside"]
        field_blank = record["Ogawa_6_FB"]
        for room_number, column in enumerate(ROOM_COLUMNS, start=1):
            room = record[column]
            per_room.append({
                "house_id": record["house_id"], "phase": record["phase"], "room": room_number,
                "outdoor_minus_fb": outdoor - field_blank,
                "indoor_minus_outdoor": room - outdoor - field_blank,
                "io_ratio": (room - field_blank) / (outdoor - field_blank),
                "notes": clean_note(record.get("Notes")),
            })
    return pd.DataFrame(per_room)


def one_number_per_house(per_room, rooms_mode):
    if rooms_mode == "all":
        return per_room
    grouped = per_room.groupby(["house_id", "phase"], as_index=False).agg(
        outdoor_minus_fb=("outdoor_minus_fb", "first"),
        indoor_minus_outdoor=("indoor_minus_outdoor", "mean"),
        io_ratio=("io_ratio", "mean"),
        notes=("notes", "first"),
    )
    return grouped


def make_boxplot(table, column, title, y_label, reference_line, output_path, join_lines, label_houses):
    figure, axes = plt.subplots(figsize=(6.5, 5.5))
    random_generator = np.random.default_rng(0)
    positions = {"pre": 0, "post": 1}

    for phase, position in positions.items():
        values = table[table["phase"] == phase][column].dropna()
        if values.empty:
            continue
        axes.boxplot(
            values, positions=[position], widths=0.5, patch_artist=True, showfliers=False,
            boxprops=dict(facecolor=PHASE_COLORS[phase]), medianprops=dict(color="black", linewidth=1.5),
        )

    pivot = table.pivot(index="house_id", columns="phase", values=column).dropna() if (join_lines or label_houses) else None
    if join_lines:
        for house_id, row in pivot.iterrows():
            axes.plot([0, 1], [row["pre"], row["post"]], color="gray", linewidth=0.8, alpha=0.6, zorder=2)
    if label_houses:
        for house_id, row in pivot.iterrows():
            axes.annotate(house_id, (1, row["post"]), xytext=(6, 0), textcoords="offset points",
                          fontsize=7, color="dimgray", va="center")
    for phase, position in positions.items():
        values = table[table["phase"] == phase][column].dropna()
        axes.scatter(position + random_generator.uniform(-0.04, 0.04, len(values)), values,
                     s=22, color="black", alpha=0.7, zorder=3)

    axes.axhline(reference_line, color="gray", linestyle="--", linewidth=1, zorder=0)
    axes.set_xticks([0, 1])
    axes.set_xticklabels(["pre", "post"])
    axes.set_xlim(-0.6, 1.7)
    axes.set_ylabel(y_label)
    axes.set_title(title)
    axes.grid(axis="y", alpha=0.3)
    axes.legend(handles=[Patch(facecolor=PHASE_COLORS["pre"], label="pre"), Patch(facecolor=PHASE_COLORS["post"], label="post")],
                loc="upper left", bbox_to_anchor=(1.01, 1))
    plt.tight_layout()
    plt.savefig(output_path, dpi=150, bbox_inches="tight")
    plt.close(figure)
    print(f"Saved {output_path}")


def build_arg_parser():
    parser = argparse.ArgumentParser(description="Ogawa NO2 box plots, pre vs post, one dot per house")
    parser.add_argument("values_file", help="The Ogawa results .xlsx file (needs an Ogawa_raw sheet)")
    parser.add_argument("--output-dir", default="ogawa_plots", help="Folder to save the table and figures into")
    parser.add_argument("--join-lines", action="store_true", help="Draw a thin line from each house's pre dot to its post dot (off by default)")
    parser.add_argument("--label-houses", action="store_true", help="Write the house number next to each post dot (off by default)")
    parser.add_argument("--rooms", choices=["mean", "all"], default="mean",
                        help="mean (default) = one number per house, the mean of its four rooms; all = every room is its own dot")
    return parser


def main():
    args = build_arg_parser().parse_args()
    os.makedirs(args.output_dir, exist_ok=True)

    values = load_ogawa_values(args.values_file)
    per_room = calculate_numbers(values)

    for _, record in values.iterrows():
        if clean_note(record.get("Notes")):
            print(f"Note: {record['house_id']} {record['phase'].upper()} -- {clean_note(record['Notes'])}")

    summary_path = os.path.join(args.output_dir, "ogawa_summary.csv")
    per_room.to_csv(summary_path, index=False)
    print(f"Saved {summary_path} (one row per house / phase / room)")

    table = one_number_per_house(per_room, args.rooms)
    can_pair = args.rooms == "mean"  # lines and labels only make sense with one dot per house
    join_lines = args.join_lines and can_pair
    label_houses = args.label_houses and can_pair
    house_count = table["house_id"].nunique()
    print(f"{house_count} houses; {'one number per house (mean of its 4 rooms)' if join_lines else 'every room is its own dot'}")

    # outdoor is one number per house and phase either way
    outdoor = per_room.groupby(["house_id", "phase"], as_index=False).agg(outdoor_minus_fb=("outdoor_minus_fb", "first"))
    make_boxplot(outdoor, "outdoor_minus_fb", "Ogawa NO2 outdoors (outdoor - FB), pre vs post",
                 "Outdoor NO2 (ppb)", 0.0, os.path.join(args.output_dir, "ogawa_outdoor_boxplot.png"), args.join_lines, args.label_houses)
    make_boxplot(table, "indoor_minus_outdoor", "Ogawa NO2 indoor minus outdoor (room - outdoor - FB), pre vs post",
                 "Indoor minus outdoor NO2 (ppb)", 0.0, os.path.join(args.output_dir, "ogawa_indoor_minus_outdoor_boxplot.png"), join_lines, label_houses)
    make_boxplot(table, "io_ratio", "Ogawa NO2 indoor / outdoor ratio, pre vs post",
                 "Indoor / outdoor NO2 (ratio)", 1.0, os.path.join(args.output_dir, "ogawa_io_ratio_boxplot.png"), join_lines, label_houses)


if __name__ == "__main__":
    main()