# Tariff model

Spot (wholesale) prices are not what a household pays. Grid fees, taxes, levies and
the supplier's margin are added, and most of them do **not** change with the hour.
That is why percentage savings measured on spot prices look much bigger than the
savings on a real bill. WattMagent therefore reports results on an all-in price.

## Formula (per kWh, per hour)
```
net   = spot + sum(fixed ct/kWh) + sum(percent_of_spot * spot) + time_of_use(local hour)
gross = net * (1 + VAT)
```
Not modelled, on purpose: monthly base fees (charging earlier or later does not
change them) and anything that depends on consumption volume or peak power.

## Component kinds
| Kind | Meaning |
|---|---|
| `fixed_per_kwh` | A constant ct/kWh, for example electricity tax or a flat grid fee |
| `percent_of_spot` | A percentage of the spot price (some suppliers charge this) |
| `time_of_use` | A schedule in **local clock hours** that tiles 0..24 exactly, for time-variable grid fees |

## Provenance rules (enforced when the file is loaded)
- Every component and the VAT rate need a `source` and a `retrieved_on` date.
- A tariff with `illustrative = false` needs a source that is a URL or
  `document:<file name>`, and values retrieved within 370 days (statutory values
  usually change on 1 January).
- A tariff with `illustrative = true` may use placeholders, but every result
  computed with it prints `ILLUSTRATIVE TARIFF: euro figures are NOT real`.
- Implausible values (above 100 ct/kWh, VAT above 30%), unknown kinds, and schedules
  with gaps or overlaps are rejected. Dates in the future are rejected.

## What is verified and what is not
| Item | Status |
|---|---|
| Electricity tax 2.05 ct/kWh (20.50 EUR/MWh, unchanged since 2003) | Secondary source (stromauskunft.de), not checked against the law |
| Concession fee, levies, average grid fee | 2025 figures from one secondary page, regionally variable, **2026 not checked** |
| VAT 19% | Standard German rate from general knowledge, not re-verified here |
| Supplier margin | Placeholder, not researched |

The shipped file `config/tariffs/illustrative_de_household.toml` is illustrative.
To get real euro figures, copy it, set `illustrative = false`, and fill in the values
from your own supplier price sheet and your grid operator's price sheet
("Preisblatt").

## Use
```
python -m wattmagent.backtest --csv data/prices_hourly_utc.csv \
    --tariff config/tariffs/illustrative_de_household.toml
```
