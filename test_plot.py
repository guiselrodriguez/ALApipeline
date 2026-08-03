"""
Stacked plot generator -- reads a standardized H<N>.csv (the output of
process_hbrl_data.py's `sensors` command) and produces one figure with
a stacked subplot for each variable you ask for.

The x-axis is always time (timestamp_pst) -- that's fixed by design,
since the whole point of stacking panels is to visually line up spikes
across different signals on the same timeline. Only the y-axis (which
variables to show) is something you choose.

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
import pandas as pd
import matplotlib.pyplot as plt


def load_series(df, instrument, variable, phase=None):
    mask = (df["instrument"] == instrument) & (df["variable"] == variable)
    if phase:
        mask &= df["phase"] == phase
    sub = df[mask].sort_values("timestamp_pst")
    if sub.empty:
        return None, None, None
    unit = sub["unit"].iloc[0]
    return sub["timestamp_pst"], sub["value"], unit


def make_stack_plot(csv_file, y_series, phase=None, output="stack_plot.png"):
    df = pd.read_csv(csv_file, parse_dates=["timestamp_pst"])

    fig, axes = plt.subplots(len(y_series), 1, figsize=(12, 3 * len(y_series)), sharex=True)
    if len(y_series) == 1:
        axes = [axes]

    for ax, (instrument, variable) in zip(axes, y_series):
        x, y, unit = load_series(df, instrument, variable, phase)
        if x is None:
            print(f"Warning: no data found for {instrument}:{variable}" +
                  (f" (phase={phase})" if phase else "") + " -- leaving that panel blank")
            ax.set_title(f"{instrument}: {variable} -- NO DATA FOUND")
            continue
        ax.plot(x, y, linewidth=0.8)
        ax.set_ylabel(f"{variable}\n({unit})" if unit else variable)
        ax.set_title(f"{instrument}: {variable}")
        ax.grid(alpha=0.3)

    axes[-1].set_xlabel("Time")
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
    parser = argparse.ArgumentParser(description="Build a stacked time-series plot from a standardized H<N>.csv file")
    parser.add_argument("csv_file", help="Path to the H<N>.csv file (output of the sensors command)")
    parser.add_argument("--y", nargs="+", required=True,
                         help='One or more "Instrument:variable" pairs, e.g. --y "Atmocube:co2" "Geocene:stove_temp_left"')
    parser.add_argument("--phase", choices=["pre", "post"], default=None,
                         help="Only plot this phase (default: both pre and post shown together)")
    parser.add_argument("--output", default="stack_plot.png", help="Output image path")
    return parser


if __name__ == "__main__":
    args = build_arg_parser().parse_args()
    y_series = [parse_series_arg(s) for s in args.y]
    make_stack_plot(args.csv_file, y_series, phase=args.phase, output=args.output)