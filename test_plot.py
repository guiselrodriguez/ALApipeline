"""
Stacked plot generator -- reads a standardized H<N>.csv (the output of
process_hbrl_data.py's `sensors` command) and produces one figure with
a stacked subplot for each variable you ask for.

The underlying table stores timestamp_utc (true UTC). This script converts
to PDT (local Oregon time) just for display -- the data itself stays in UTC,
only the plot x-axis is shown in PDT.

When both pre and post are shown together (no --phase flag), each panel
gets a vertical line marking the excluded visit window, plus "pre"/"post"
labels, so it's obvious at a glance where the intervention happened.

The x-axis is always time -- that's always fixed, since the whole point
of stacking panels is to visually line up spikes across different signals
on the same timeline. Only the y-axis (which variables to show) is
something you choose.

Because several instruments measure the same thing (e.g. co2 shows up
from Atmocube, AirAssure, AND Aranet), each y-axis series needs to be
specified as "instrument:variable", not just the variable name alone --
otherwise the script wouldn't know which instrument's co2 you meant.

Usage:
    python test_plot.py <H<N>.csv> --y "Atmocube:co2" "Geocene:stove_temp_left" "Anemometer:hood_airflow"
    python test_plot.py <H<N>.csv> --y "Atmocube:co2" --phase pre --output my_plot.png
"""

import argparse
import sys
from datetime import timedelta
import pandas as pd
import matplotlib.pyplot as plt

AVAILABLE_SERIES = {
    "Atmocube": ["co2", "pm1", "pm25", "pm4", "pm10", "temperature", "humidity",
                 "absolute_humidity", "pressure", "noise", "light", "ch2o", "voc_index", "nox_index"],
    "AirAssure": ["co2", "co", "no2", "o3", "so2", "pm1", "pm25", "pm4", "pm10",
                  "temperature", "humidity", "pressure", "etoh", "tvoc"],
    "Geocene": ["stove_temp_left", "stove_temp_right"],
    "Hobo": ["power"],
    "Anemometer": ["hood_airflow"],
    "Kestrel": ["temperature", "humidity"],
    "Aranet": ["co2", "temperature", "humidity", "pressure"],
}


def usage_banner():
    lines = [
        "",
        "HOW TO USE THIS SCRIPT",
        "-----------------------",
        "Example:",
        '  python test_plot.py H1.csv --y "Atmocube:co2" "Geocene:stove_temp_left" "Anemometer:hood_airflow"',
        "",
        "Each --y entry is written as \"Instrument:variable\" (see the list below for valid options).",
        "Every entry becomes its own stacked panel, sharing the same time axis (shown in PDT).",
        "",
        "Optional flags:",
        "  --phase pre        only show the pre period",
        "  --phase post        only show the post period",
        "  --output name.png   name the saved image (default: stack_plot.png)",
        "",
        "Available Instrument:variable options:",
    ]
    for instrument, variables in AVAILABLE_SERIES.items():
        lines.append(f"  {instrument}: " + ", ".join(variables))
    lines.append("")
    return "\n".join(lines)


def load_series(df, instrument, variable, phase=None):
    mask = (df["instrument"] == instrument) & (df["variable"] == variable)
    if phase:
        mask &= df["phase"] == phase
    sub = df[mask].sort_values("timestamp_pst_display")
    if sub.empty:
        return None, None, None
    unit = sub["unit"].iloc[0]
    return sub["timestamp_pst_display"], sub["value"], unit


def mark_pre_post_boundary(ax, df, show_labels=False):
    """Draws a red dashed line marking where pre ends and post begins,
    and (only on the bottom panel) labels 'pre'/'post' below the x-axis.
    Only makes sense when both phases are present (no --phase filter)."""
    pre_rows = df[df["phase"] == "pre"]["timestamp_pst_display"]
    post_rows = df[df["phase"] == "post"]["timestamp_pst_display"]
    if pre_rows.empty or post_rows.empty:
        return

    pre_start, pre_end = pre_rows.min(), pre_rows.max()
    post_start, post_end = post_rows.min(), post_rows.max()
    gap_mid = pre_end + (post_start - pre_end) / 2

    ax.axvline(gap_mid, color="red", linestyle="--", linewidth=1.5, alpha=0.8, zorder=0)

    if show_labels:
        pre_center = pre_start + (pre_end - pre_start) / 2
        post_center = post_start + (post_end - post_start) / 2
        ax.text(pre_center, -0.15, "pre", transform=ax.get_xaxis_transform(),
                ha="center", va="top", color="dimgray", fontsize=9, clip_on=False)
        ax.text(post_center, -0.15, "post", transform=ax.get_xaxis_transform(),
                ha="center", va="top", color="dimgray", fontsize=9, clip_on=False)


def make_stack_plot(csv_file, y_series, phase=None, output="stack_plot.png"):
    df = pd.read_csv(csv_file, parse_dates=["timestamp_utc"])
    # PDT (local Oregon time), for display only -- the CSV itself stays in true UTC
    df["timestamp_pst_display"] = df["timestamp_utc"] - timedelta(hours=7)

    fig, axes = plt.subplots(len(y_series), 1, figsize=(12, 3 * len(y_series)), sharex=True)
    if len(y_series) == 1:
        axes = [axes]

    for i, (ax, (instrument, variable)) in enumerate(zip(axes, y_series)):
        x, y, unit = load_series(df, instrument, variable, phase)
        if x is None:
            print(f"Warning: no data found for {instrument}:{variable}" +
                  (f" (phase={phase})" if phase else "") + " -- leaving that panel blank")
            ax.set_title(f"{instrument}: {variable} -- NO DATA FOUND")
            continue
        ax.plot(x, y, linewidth=0.8, zorder=2)
        ax.set_ylabel(f"{variable}\n({unit})" if unit else variable)
        ax.set_title(f"{instrument}: {variable}")
        ax.grid(alpha=0.3)

        if not phase:
            series_mask = (df["instrument"] == instrument) & (df["variable"] == variable)
            mark_pre_post_boundary(ax, df[series_mask], show_labels=(i == len(axes) - 1))

    axes[-1].set_xlabel("Time (PDT)")
    if phase:
        fig.suptitle(f"Phase: {phase}", fontsize=13, fontweight="bold")
    plt.tight_layout()
    plt.savefig(output, dpi=150)
    print(f"Saved {output}")
    return output


def parse_series_arg(raw):
    if ":" not in raw:
        print(f"Error: '{raw}' isn't in 'Instrument:variable' format, e.g. 'Atmocube:co2'")
        sys.exit(1)
    instrument, variable = raw.split(":", 1)
    return instrument, variable


def build_arg_parser():
    parser = argparse.ArgumentParser(
        description="Build a stacked time-series plot from a standardized H<N>.csv file",
        epilog=usage_banner(),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("csv_file", help="Path to the H<N>.csv file (output of the sensors command)")
    parser.add_argument("--y", nargs="+", required=True,
                         help='One or more "Instrument:variable" pairs, e.g. --y "Atmocube:co2" "Geocene:stove_temp_left"')
    parser.add_argument("--phase", choices=["pre", "post"], default=None,
                         help="Only plot this phase (default: both pre and post shown together)")
    parser.add_argument("--output", default="stack_plot.png", help="Output image path")
    return parser


if __name__ == "__main__":
    print(usage_banner())
    if len(sys.argv) == 1:
        sys.exit(0)
    args = build_arg_parser().parse_args()
    y_series = [parse_series_arg(s) for s in args.y]
    make_stack_plot(args.csv_file, y_series, phase=args.phase, output=args.output)