# Data

## Real data (to do on your machine; I could not verify sources from my sandbox)
Candidate public sources for German day-ahead prices and weather:
Bundesnetzagentur SMARD, Energy-Charts, ENTSO-E Transparency Platform, Open-Meteo.
Before using any of them, check **current terms of use, attribution rules and rate
limits**, and record them below.

| Source | URL | Licence / terms | Checked on |
|---|---|---|---|
| (fill in) | | | |

I believe EU day-ahead products moved to 15-minute resolution in October 2025.
Verify this; `load_csv` handles both hourly and 15-minute rows.

## CSV format expected by `load_csv`
```
timestamp,price_eur_mwh
2025-01-01T00:00:00,85.20
2025-01-01T01:00:00,79.10
```
- ISO 8601 timestamps, one clock convention throughout (UTC recommended).
- EUR/MWh; converted to EUR/kWh internally.
- Sub-hourly rows are averaged per hour; each hour must have the same row count.
- Gaps and duplicate hours raise `ValueError`.

## What a price is not
Day-ahead market prices are not what a household pays. Taxes, grid fees and
supplier margins differ by tariff. Model them explicitly, with sources, before
quoting any euro figure.

## Synthetic data
`synthetic_prices` is seeded and deterministic, for tests only. Output derived
from it must carry the label SYNTHETIC.
