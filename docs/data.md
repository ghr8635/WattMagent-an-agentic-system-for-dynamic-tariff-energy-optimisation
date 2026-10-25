# Data

## Primary source: SMARD (Bundesnetzagentur)

| Item | Value |
|---|---|
| Page | https://www.smard.de/en/all-about-our-data-download-section-210130 |
| Licence | CC BY 4.0: free to use, credit the source (stated on the download page) |
| Required credit | "Bundesnetzagentur | SMARD.de" (CC BY 4.0) |
| Series used | Market -> day-ahead price, Germany/Luxembourg |
| Downloaded on | (fill in) |
| Period / resolution chosen | (fill in) |

### Observed file format (English-language export)
- semicolon separated; header row first; one price column per country or zone
- `Start date;End date;...` with dates like `Jan 1, 2024 12:00 AM` (12-hour clock)
- price column: `Germany/Luxembourg [€/MWh] Calculated resolutions`
- a missing value is written as `-`
- timestamps appear to be German local time, so the converter assumes Europe/Berlin.
  The converter proves this on every run: after conversion the UTC series must be
  perfectly regular, which fails if the assumption is wrong. It cannot detect a wrong
  zone in a file that contains no clock change, so download a range that does
  (for example Jan 2024 to today).

### Convert and run
```
python -m wattmagent.data.smard data/raw_smard_prices.csv data/prices_hourly_utc.csv
python -m wattmagent.backtest --csv data/prices_hourly_utc.csv
```
The converter fails loudly on: missing prices, gaps, repeated timestamps, mixed
resolutions, partial hours, an unexpected header (for example a German-language
export), or a missing price column. Data files are git-ignored (`data/*.csv`);
re-create them with the steps above.

## Resolution
The EU day-ahead market moved to 15-minute products for delivery from
1 October 2025 (confirmed by the SDAC announcements). Data before that date is
hourly, after it quarter-hourly. SMARD lets you choose a resolution for the whole
download. Use one resolution per file; the converter averages 15-minute rows to
hourly and rejects files that mix both.

## Other sources (not yet checked)
Energy-Charts (Fraunhofer ISE) API: reported to need no registration, zone `DE-LU`,
EUR/MWh; its endpoint details and licence terms are **not verified** here. ENTSO-E
Transparency Platform: needs a registered token; terms not checked. Check terms
before using either, and record them here.

## What a price is not
Day-ahead market prices are not what a household pays. Taxes, grid fees and
supplier margins differ by tariff. Model them explicitly, with sources, before
quoting any euro figure.

## CSV format used inside WattMagent (`load_csv`)
```
timestamp,price_eur_mwh
2024-01-01T00:00:00+00:00,0.1000
```
UTC timestamps with offset, EUR/MWh, hourly, gap-free. Plug-in and departure hours
in the backtest are interpreted in the zone given by `--tz` (default Europe/Berlin),
so daylight-saving nights are 12 or 14 hours long, as in real life.

## Synthetic data
`synthetic_prices` is seeded and deterministic, for tests only. Output derived
from it must carry the label SYNTHETIC.
