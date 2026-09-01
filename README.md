# ALA Pipeline

This script takes the raw sensor data from each house and turns it into clean, standardized tables we can actually use for analysis.

## What it does

One script, four things it can do:

- **sensors** — combines all the raw instrument files for a house into one table. Converts every timestamp to UTC, tags each reading pre or post, adds sensor IDs.
- **house-charac** — builds the house characteristics table straight from the spreadsheet.
- **ogawa** — builds the Ogawa NO2 table from the Ogawa_raw tab.
- **verify-timezones** — prints raw timestamps next to what they got converted to, so you can sanity check the conversion is doing what it should.

## How to run it

Sensors (needs the house characteristics file, the pre folder/zip, and the post folder/zip):
```
python process_hbrl_data.py sensors "House Characteristics.xlsx" "H1_PRE.zip" "H1_POST.zip" --sensor-ids "Sensors_used_in_houses.xlsx" --output "output"
```

House characteristics:
```
python process_hbrl_data.py house-charac "House Characteristics.xlsx" "output"
```

Ogawa:
```
python process_hbrl_data.py ogawa "Ogawa Values.xlsx" "Ogawa.xlsx" "output"
```

Verify timezones:
```
python process_hbrl_data.py verify-timezones "H1_PRE.zip" --samples 5
```

Plot (separate script, test_plot.py):
```
python test_plot.py "output\H1.csv" --y "Atmocube:co2" "Geocene:stove_temp_left" --output demo.png
```

## File naming

Files show up in 3 different naming styles across houses, and the script checks for all of them:
1. New: `h1_pre_atmocube.csv`
2. Old: `h1_w1_atmocube.csv`
3. Legacy: `house1_week2_geocene_left.json`

Doesn't matter which one a file uses. Whatever matches just gets used to group the files together — it does NOT decide if a reading is pre or post. That's always figured out from the actual timestamp compared against the visit dates in House_charac. This is why a file labeled "post" can still contribute real "pre" rows if it happens to contain earlier data too (this actually happens a lot — several instruments export their whole deployment history into every file, not just the period the filename claims).

Also doesn't matter if files are flat in one folder or split into subfolders per instrument. Zips work too, no need to extract first.

## Timezones

- Atmocube, Hobo, AirAssure — all label their own timezone in the raw file, so these just convert straight to UTC, no guessing.
- Geocene — if the raw timestamp ends in "Z" it's already UTC. If not, it's treated as local Oregon time (PDT) and converted. Checked per file, not assumed.
- Kestrel, Aranet, Anemometer — always local Oregon time (PDT, UTC-7), converted to UTC.
- House_charac's visit dates get the same PDT conversion.

## Things worth knowing

- Anything in a folder with "processed" or "metrics" in the path gets skipped automatically. This keeps someone's already-processed output, or raw device export dumps, from accidentally getting used as real data.
- If Geocene's clean json is missing for a house, the script tries to recover the data from the raw device export directly (missions.csv + metrics/*.json.gz) — works whether that raw export is still zipped or already unzipped loose in the folder.
- AirAssure's column names and timestamp format vary between houses — the script handles multiple formats automatically instead of assuming one.
- AirAssure error codes are bitmask values (can mean more than one thing stacked together) — the script decodes them into plain text instead of just showing the raw number. "Cloud disconnected" is expected and doesn't get flagged, since the sensors aren't on WiFi.
- Kestrel files sometimes have a few extra metadata rows before the real header starts, and use different column names between deliveries — the script scans for the real header instead of assuming it's line 1.
- Anemometer files use either commas or tabs between fields depending on the export — both are handled automatically.

## test_plot.py

Reads a house's output csv and builds a stacked plot for whatever variables you ask for. When both pre and post are shown together, a red dashed line marks where the intervention happened, with "pre"/"post" labeled underneath.
