# HBRL house data pipeline — session handoff

Paste this whole file into a new conversation to resume where we left off.
Everything below reflects a single work session; treat file paths as
correct for the machine they were written on and confirm they still
resolve on the new machine before trusting them blindly.

## Who/what this is

The user works with a research team doing indoor air-quality studies. Each
house is visited twice: 7 days in the kitchen recording data with ~9
instrument types, then the researchers leave, then a second visit of the
same length (not always 7 days — house 1 has two 7-day visits, house 2 has
two 10-day visits, per the boss). The user's job is turning the raw
per-instrument exports into one standardized table per house-visit.

The design doc (the source of truth) is a PDF: `Summer Project HBRL (1).pdf`,
found in
`/Users/work/Downloads/guiselproject/drive-download-20260727T061000Z-1-001/`.
Re-read it if anything below is ambiguous — it's short (3 pages).

## Target schema (from the design doc)

```
house_id, date, timestamp_pst, instrument, variable, value, unit, qc_flag
```
- `timestamp_pst` is explicitly defined as **fixed UTC-8**, not DST-aware
  Pacific time.
- Two output tables are wanted: one for the pre-visit (week 1) and one for
  the post-visit (week 2) — i.e. run the pipeline once per week folder, it
  is not meant to merge both weeks into one file.
- 9 instrument types: Atmocube, AirAssure, Geocene, Hobo, Anemometer,
  Kestrel, Aranet, Ogawa, House_charac. Ogawa and House_charac have not
  appeared in any sample data yet — parsers for them are stubs.

## Boss's clarifications (verbatim, given mid-session)

1. "No, there is no fixed period. For example, house 1 have two 7-days
   period and house 2 have two 10-days periods." → the pipeline must not
   hardcode a visit length.
2. "For the airassure, flag the error, like print it in the prompt like
   House 2 Week 2 at 12:34 PM had an error. Also, if you could flag that
   there's no data between a certain period, like no data on date 07/07
   between 11:30 PM and 11:45 PM" → console-printed error/gap detection,
   not just written into the CSV.
3. "Yes, hobo is present only on week 2 for the induction stove."
4. AirAssure will always show system status code 4 ("cloud disconnected")
   because it's never connected to wifi — that one code is expected/benign
   and should NOT be printed as an error. Any other code should be.

## Where the raw data lives

- Week 1 (house 1): `/Users/work/Downloads/guiselproject/drive-download-20260727T061000Z-1-001/`
  — files named `h1_w1_<instrument>.csv/txt/json`, plus the design PDF and
  two `.zip` files (`9ddf0a11-...zip`, `9d49b2ef-...zip`) which are raw
  Geocene cloud exports (missions.csv/sensors.csv/metrics/*.json.gz) — these
  turned out to be full ~15-day dumps, already superseded by the cleaner
  `h1_w1_geocene_left/right.json` files, so **they are not used** by the
  pipeline. Don't re-investigate them unless a new house's Geocene data
  shows up only as a zip.
- Week 2 (house 1): `/Users/work/Downloads/guiselproject/drive-download-20260727T060901Z-1-001/`
  — files named `h1_w2_<instrument>.csv/txt/json`.
- No house 2 data has been provided yet.

## The pipeline

- Script: `/Users/work/Downloads/guiselproject/pipeline/process_hbrl_data.py`
- Docs: `/Users/work/Downloads/guiselproject/pipeline/README.md` (points at
  the same findings as this file, written for the boss/team, less session
  narrative)
- Output: `/Users/work/Downloads/guiselproject/pipeline/output/` (created
  automatically next to the script — NOT inside the raw data folders)

Usage:
```bash
python3 process_hbrl_data.py <input_folder> [output_folder]
python3 process_hbrl_data.py <input_folder> [output_folder] --start "2026-05-15 12:00:00" --days 7
```
Run once per week folder. Auto-detects every `h<N>_w<M>_...` group present.

Current real output (regenerate any time — it's deterministic):
```bash
python3 "/Users/work/Downloads/guiselproject/pipeline/process_hbrl_data.py" "/Users/work/Downloads/guiselproject/drive-download-20260727T061000Z-1-001"
python3 "/Users/work/Downloads/guiselproject/pipeline/process_hbrl_data.py" "/Users/work/Downloads/guiselproject/drive-download-20260727T060901Z-1-001"
```
Last confirmed result: 428,823 rows for week 1, 493,422 rows for week 2,
all `qc_flag=ok` (house 1's sample data genuinely has no AirAssure errors —
verified by grepping the raw status columns directly, not just trusting the
pipeline).

## Big data-quality findings this session (important — will recur with house 2)

1. **Several raw exports contain the FULL multi-week recording in BOTH week
   folders, not just that week's slice.** Verified by MD5 checksum: AirAssure
   pt1/pt2/pt3 are byte-identical between the week-1 and week-2 folders.
   Kestrel has the same full date range in both folders too (not
   byte-identical only because two of its columns are reordered between the
   two copies — same underlying readings). Week-2's Atmocube and Geocene
   files were also found to contain the whole ~15-day span instead of just
   week 2.
2. **Only the Anemometer file was reliably pre-trimmed to the real visit in
   both weeks** (it's physically installed/removed with the visit). So the
   pipeline derives a "visit window" per house-week from
   `min()`/`max()` of that folder's Anemometer timestamps, and trims every
   other instrument's readings to fall inside it before writing output.
   This is a **heuristic, not a guarantee** — if some other house's
   Anemometer export also turns out to be a full multi-week dump, the
   window would be silently wrong. The user was made aware of this risk
   explicitly. No better ground-truth source (e.g. an actual visit-date
   field in `House_charac`) has been found yet because no `House_charac`
   sample file has been provided.
3. Because of finding 1/2, added a manual override: `--start "<datetime>"
   --days <N>` on the command line replaces the auto-detected window. Console
   always prints the auto-detected window AND, if used, the override, so
   nothing is silently swapped without the user seeing both.
4. **Atmocube's own `timestamp_pst` column is mislabeled** — cross-checked
   against its `ts` (unix epoch / true UTC) column, it's actually UTC-7
   (Pacific Daylight Time), not the fixed UTC-8 the schema requires. The
   pipeline ignores Atmocube's built-in `timestamp_pst`/`date`/`time`
   columns and recomputes from `ts` instead.
5. **Timezone conversion could only be verified for instruments with an
   explicit UTC anchor**: AirAssure (header literally says `UTC`), Geocene
   (raw `...Z` timestamps), Hobo (header says `GMT-07:00`). Those three are
   actively converted to fixed UTC-8. Kestrel, Aranet, and Anemometer carry
   no verifiable UTC reference in the file itself, so they're passed
   through as recorded, on the assumption the device clock was already
   correct — flagged as an assumption, not verified.
6. **AirAssure status codes**: the design doc's "common codes" list (1, 2,
   4, 8, 16, 32, 64) is incomplete. Fetched the full list from
   https://tsi.com/resources/airassure-iaq-monitors-faqs and confirmed two
   important subtleties:
   - Bit `256` means different things depending on which status column:
     `fan_rpm_error` on PM Status vs. `sensor_unsupported` on VOC Status.
     The pipeline decodes each status column (`PM Status`, `VOC Status`,
     everything else) with its own bitmap rather than one shared map
     (`STATUS_COL_BITMAP` in the script).
   - PM Status also has `512` (laser error) and `2048` (cleaning cycle
     completed — informational, not a fault, but still surfaced since the
     doc's rule is "0 = good, anything else = bad"); System Status has
     three more codes beyond the doc (`16` time not synced, `32` time
     invalid, `64` EEPROM failure).
   - Any bit not covered by these maps is still surfaced as
     `unrecognized_status_bit_<n>` rather than silently dropped.
   - Verified this whole subsystem with a synthetic AirAssure file
     (status 1027 = 1024+2+1, matching the design doc's own worked example)
     before trusting it against real data — the first version of the code
     had a bug here (missing bit 1024 entirely) that the synthetic test
     caught.
7. Some Atmocube units (`voc`, `abs_h`, `p`, `noise`, `light`, `ch2o`,
   `voc_index`, `nox_index`) are reasonable-default guesses, not confirmed
   by the design doc (only `co2`, `pm25`, `temperature` were given
   explicitly in its sample table). Worth a sanity check against the
   Atmocube spec sheet at some point.

## What the script does per instrument (quick reference)

- **Atmocube**: clean CSV, but recompute `timestamp_pst` from `ts` (unix
  epoch) rather than trusting the file's own mislabeled column. Emits ~15
  variables per row (voc, pm1/2.5/4/10, co2, temperature, humidity,
  absolute_humidity, pressure, noise, light, ch2o, voc_index, nox_index).
  Blank/warmup rows are simply skipped (no value = no row emitted).
- **AirAssure**: combine pt1+pt2+pt3 in that order, strip `#` preamble
  lines and repeated header/units rows wherever they occur mid-file, parse
  UTC timestamps → fixed UTC-8. Per-row: decode System Status (bit 4 =
  benign/expected, everything else = real error, printed to console) and
  per-variable component status columns (see finding #6 above) into
  `qc_flag`; console-prints one combined error line per timestamp that had
  any real error, and a gap-detection warning if the sampling interval
  blows out.
- **Geocene**: `_left.json` / `_right.json`, raw UTC timestamps → fixed
  UTC-8. Straightforward — sensor_type_id 1 = Celsius k-type thermocouple,
  matches the design doc's example values exactly.
- **Hobo**: week 2 only. Header says `GMT-07:00` (true PDT) — converted to
  UTC then to fixed UTC-8 (net effect: subtract 1 hour from the recorded
  local time). Only the "Active Power, W" column is emitted as `power`
  (W); the event columns (Started/Stopped/Line Loss/etc.) are ignored.
- **Anemometer**: skip the 6-line file-info header, parse
  `idx, value, unit, DD-MM-YYYY, HH:MM:SS` rows. Its own min/max timestamps
  *are* the auto-detected visit window (see finding #2), so it also
  defines the window it then gets trimmed to (trivially: nothing gets
  dropped from Anemometer itself under auto-detection, only under a
  manual override).
- **Kestrel**: reads columns by name via `csv.DictReader` (column order
  differs between the two weeks' files, per finding #1), trusts
  `timestamp_pst` as recorded, uses the `Temperature(C)` column directly
  if present else converts from F.
- **Aranet**: `DD/MM/YYYY h:mm:ss AM/PM` timestamp format (confirmed by
  checking for day values >12 that wouldn't work as MM/DD). Converts F→C
  only when the header says `°F` — week 1's file was Fahrenheit, week 2's
  was already Celsius, so this must stay dynamic, not hardcoded per week.
- **Ogawa / House_charac**: not present in any sample data. Ogawa parser is
  a no-op stub. House_charac is explicitly treated as static per-house
  metadata, not a timeseries — even once a sample shows up, the plan is to
  print/copy it aside rather than force it into the long table (join on
  `house_id` at analysis time instead).

## Explicitly NOT done yet

**Cooking event detection** (page 3 of the design PDF) has not been
started. It wants: threshold each of stove burner temp (Geocene), induction
power (Hobo, week 2 only), and range hood airflow (Anemometer) into
time-windows of activity; combine overlapping windows across signals as
stronger/weaker evidence of a cooking event; then check whether
pollutant spikes (CO2/PM/NO2 from Atmocube/AirAssure/etc.) fall inside
those windows. The PDF itself says thresholds need "(get clarification)" —
this was intentionally deferred rather than guessed. The user was last
asked whether to tackle this next; no answer had been given before this
handoff was written.

## Suggested first message on the new machine

"I'm continuing the HBRL data pipeline project — read
`/Users/work/Downloads/guiselproject/pipeline/HANDOFF.md` [adjust path if
it moved] for full context, then [pick up cooking event detection / re-run
against new house 2 data / whatever the actual next ask is]."
