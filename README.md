# HBRL house data pipeline

`process_hbrl_data.py` converts one folder of raw instrument exports into the
standardized long table:

```
house_id, date, timestamp_pst, instrument, variable, value, unit, qc_flag
```

## Usage

Run it once per week/visit folder (this is the "two batches" workflow):

```
python3 process_hbrl_data.py "/path/to/week1_folder"
python3 process_hbrl_data.py "/path/to/week2_folder"
```

Each run auto-detects every house/week present from filenames of the form
`h<N>_w<M>_<instrument>...`, and writes one CSV per house-week into
`pipeline/output/` (created automatically next to this script -- pass a
second argument to write elsewhere instead), named `h<N>_w<M>_processed.csv`.
It does not assume a fixed 7-day period, so the same script works for a
house with a 10-day visit.

Console output while it runs reports (per house/week):
- the visit window it detected (from the Anemometer file)
- how many readings from each instrument got trimmed as outside that window

If the auto-detected window looks wrong for a given house/week, override it
manually with `--start` and `--days` (period length):

```
python3 process_hbrl_data.py "/path/to/week1_folder" --start "2026-05-15 12:00:00" --days 7
```

Both flags are required together. When given, they replace the
Anemometer-derived window (still printed for comparison) for trimming every
instrument in that run.
- any AirAssure error (status code other than the expected "4 = cloud
  disconnected, no wifi"), decoded from the bitmask, e.g.:
  `House 2 Week 2 AirAssure at 12:34 PM on 2026-05-20 had an error: pm25: hardware_fault`
- any gap in an instrument's readings notably larger than its normal sample
  interval, e.g.: `House 1 Week 1 AirAssure: no data on 05/20 between 11:30 PM and 11:45 PM`

## Data-quality findings from the sample data (house 1, weeks 1 & 2)

These are worth confirming with whoever manages the instruments, since they
affect every house:

1. **Several raw exports contain the FULL multi-week recording, not just
   that week's slice.** AirAssure (pt1/2/3) and Kestrel are byte-identical
   between the week-1 and week-2 folders — both contain the entire ~15-day
   span. Atmocube and Geocene are properly trimmed in the week-1 folder but
   contain the full span in the week-2 folder. Only **Anemometer** (and
   Hobo, which only exists for week 2) were reliably pre-trimmed to the
   actual visit in both folders — consistent with it being physically
   installed/removed with the visit. The pipeline therefore uses each
   folder's Anemometer min/max timestamp as the "visit window" and trims
   every other instrument to it. If a house's Anemometer file is missing or
   corrupt, nothing gets trimmed and you'll see a warning printed — worth
   watching for.

2. **Atmocube's own `timestamp_pst` column is mislabeled.** Cross-checking
   its `ts` (unix epoch / true UTC) column against the file's own
   `timestamp_pst` column shows a 7-hour offset, i.e. that column is
   actually Pacific *Daylight* Time (UTC-7), not the fixed UTC-8 the design
   doc defines `timestamp_pst` to mean. The pipeline ignores Atmocube's
   built-in `timestamp_pst`/`date`/`time` columns and recomputes the
   timestamp from `ts` instead.

3. **Timezone conversion could only be verified for instruments with an
   explicit UTC anchor**: AirAssure (header says `UTC`), Geocene (raw `Z`
   timestamps), and Hobo (header says `GMT-07:00`). Those three are
   actively converted to fixed UTC-8. Kestrel, Aranet, and Anemometer carry
   no verifiable UTC reference in the file itself, so their timestamps are
   passed through as recorded, on the assumption the device clocks were
   already set correctly. If that assumption is wrong for a given
   deployment, those three would be off by whatever the device clock drift
   was.

4. **AirAssure status codes use the full list from
   [TSI's AirAssure FAQ page](https://tsi.com/resources/airassure-iaq-monitors-faqs),**
   not just the "common codes" subset in the design doc. Notably:
   - Bit `256` means different things depending on which status column it's
     in — `fan_rpm_error` for PM Status, `sensor_unsupported` for VOC
     Status — so the pipeline decodes each status column with its own
     bitmap rather than one shared map.
   - PM Status also has `512` (laser error) and `2048` (cleaning cycle
     completed, informational rather than a fault) in addition to `1024`
     (fan blocked, which the design doc only shows in its worked example,
     1027 = 1024+2+1).
   - System Status has three more codes beyond the doc's list: `16` (time
     not synced to cloud in 24h), `32` (time invalid), `64` (EEPROM
     failure).
   Any bit not covered by these maps is still surfaced as
   `unrecognized_status_bit_<n>` in `qc_flag` rather than silently dropped.

5. **Ogawa and House_charac** were not present in the sample data, so their
   parsers are stubs. House_charac is static per-house metadata (not a
   timeseries), so even when present it's not merged into the long table —
   it should be joined on `house_id` at analysis time instead.

6. **Some Atmocube units are inferred, not confirmed.** The design doc's
   sample table confirms `co2` (ppm), `pm25` (ug/m3), and `t`→`temperature`
   (C). The other Atmocube columns (`voc`, `abs_h`, `p`, `noise`, `light`,
   `ch2o`, `voc_index`, `nox_index`) use units that are reasonable defaults
   for that class of sensor but weren't given explicitly — worth a sanity
   check against the Atmocube spec sheet.
