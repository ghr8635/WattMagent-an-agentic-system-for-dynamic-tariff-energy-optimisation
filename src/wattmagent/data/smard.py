"""Convert a SMARD day-ahead price export to WattMagent's CSV format.

Observed export format (English-language SMARD download, checked against a real file):
  - semicolon separated, one price column per country/zone, header row first
  - ``Start date`` / ``End date`` like ``Jan 1, 2024 12:00 AM`` (12-hour clock, English)
  - the column we need is named like ``Germany/Luxembourg [€/MWh] Calculated resolutions``
  - a missing value is written as ``-``
  - timestamps are German local time (assumed Europe/Berlin; verified, see below)

Output: ``timestamp,price_eur_mwh`` with UTC ISO timestamps, one row per hour, ready
for ``wattmagent.data.prices.load_csv``.

Safety rules (everything fails loudly instead of guessing):
  - one uniform resolution per file (15 or 60 minutes); mixed files are rejected
  - local times are converted to UTC, resolving the repeated hour in October by order
  - after conversion the UTC series must be strictly regular, with no gaps or repeats;
    if the file is not really in the assumed time zone this check fails
  - any missing price in the chosen column is an error
"""

from __future__ import annotations

import argparse
import csv
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

_MONTHS = {
    m: i
    for i, m in enumerate(
        ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1
    )
}
_DATE = re.compile(r"^([A-Za-z]{3}) (\d{1,2}), (\d{4}) (\d{1,2}):(\d{2}) (AM|PM)$")
DEFAULT_COLUMN_PREFIX = "Germany/Luxembourg"


@dataclass(frozen=True, slots=True)
class ConversionReport:
    rows_read: int
    resolution_minutes: int
    hours_written: int
    first_utc: datetime
    last_utc: datetime
    repeated_local_hours_resolved: int
    negative_price_hours: int
    min_eur_mwh: float
    max_eur_mwh: float


def parse_smard_date(text: str) -> datetime:
    """Parse ``Jan 1, 2024 12:00 AM`` without depending on the machine's locale."""
    m = _DATE.match(text.strip())
    if not m or m.group(1) not in _MONTHS:
        raise ValueError(f"unrecognised SMARD date: {text!r}")
    mon, day, year, hour, minute, ampm = m.groups()
    h = int(hour)
    if not 1 <= h <= 12:
        raise ValueError(f"bad 12-hour clock value in {text!r}")
    h = (0 if h == 12 else h) + (12 if ampm == "PM" else 0)
    return datetime(int(year), _MONTHS[mon], int(day), h, int(minute))


def _parse_price(text: str) -> float | None:
    t = text.strip().replace(",", "")  # tolerate thousands separators like 1,234.56
    if t in ("", "-"):
        return None
    return float(t)


def _find_column(header: list[str], prefix: str) -> int:
    for i, name in enumerate(header):
        if name.strip().startswith(prefix):
            return i
    raise ValueError(f"no column starting with {prefix!r}; columns are: {header}")


def convert_smard_csv(
    src: Path,
    dst: Path,
    column_prefix: str = DEFAULT_COLUMN_PREFIX,
    tz_name: str = "Europe/Berlin",
) -> ConversionReport:
    tz = ZoneInfo(tz_name)
    with src.open(newline="", encoding="utf-8-sig") as f:
        rows = list(csv.reader(f, delimiter=";"))
    if len(rows) < 2:
        raise ValueError("file has no data rows")
    header, data = rows[0], rows[1:]
    if len(header) < 3 or header[0].strip() != "Start date":
        raise ValueError(f"unexpected header, expected 'Start date;End date;...': {header[:3]}")
    col = _find_column(header, column_prefix)

    first_start, first_end = parse_smard_date(data[0][0]), parse_smard_date(data[0][1])
    minutes = int((first_end - first_start).total_seconds() // 60)
    if minutes not in (15, 60):
        raise ValueError(f"unsupported resolution of {minutes} minutes in first row")

    seen_utc: set[datetime] = set()
    points: list[tuple[datetime, float]] = []
    repeated = 0
    for line_no, row in enumerate(data, start=2):
        if not row or not row[0].strip():
            continue
        price = _parse_price(row[col])
        if price is None:
            raise ValueError(f"missing price at line {line_no} ({row[0]}); re-download or fill it")
        local = parse_smard_date(row[0]).replace(tzinfo=tz)
        utc0 = local.replace(fold=0).astimezone(UTC)
        utc1 = local.replace(fold=1).astimezone(UTC)
        if utc0 in seen_utc and utc1 != utc0:
            utc, repeated = utc1, repeated + 1  # second pass through the repeated hour
        else:
            utc = utc0
        if utc in seen_utc:
            raise ValueError(f"duplicate timestamp at line {line_no} ({row[0]})")
        seen_utc.add(utc)
        points.append((utc, price))

    step = timedelta(minutes=minutes)
    for (a, _), (b, _) in zip(points, points[1:], strict=False):
        if b - a != step:
            raise ValueError(
                f"series is not regular at {a.isoformat()} -> {b.isoformat()} "
                f"(expected steps of {minutes} min). Mixed resolutions, a gap, or the file "
                f"is not in {tz_name}; try --tz UTC."
            )

    per_hour = 60 // minutes
    buckets: dict[datetime, list[float]] = defaultdict(list)
    for t, p in points:
        buckets[t.replace(minute=0)].append(p)
    if any(len(v) != per_hour for v in buckets.values()):
        raise ValueError("partial hour at the start or end of the file; trim to whole hours")

    hours = sorted(buckets)
    out = [(h, sum(buckets[h]) / per_hour) for h in hours]
    dst.parent.mkdir(parents=True, exist_ok=True)
    with dst.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["timestamp", "price_eur_mwh"])
        for h, p in out:
            w.writerow([h.isoformat(), f"{p:.4f}"])

    values = [p for _, p in out]
    return ConversionReport(
        rows_read=len(points),
        resolution_minutes=minutes,
        hours_written=len(out),
        first_utc=hours[0],
        last_utc=hours[-1],
        repeated_local_hours_resolved=repeated,
        negative_price_hours=sum(v < 0 for v in values),
        min_eur_mwh=min(values),
        max_eur_mwh=max(values),
    )


def main() -> None:
    p = argparse.ArgumentParser(description="Convert a SMARD price export to WattMagent CSV")
    p.add_argument("src", type=Path)
    p.add_argument("dst", type=Path)
    p.add_argument("--column", default=DEFAULT_COLUMN_PREFIX, help="price column name prefix")
    p.add_argument("--tz", default="Europe/Berlin", help="time zone of the file's timestamps")
    a = p.parse_args()
    r = convert_smard_csv(a.src, a.dst, a.column, a.tz)
    print(f"read {r.rows_read} rows at {r.resolution_minutes}-minute resolution")
    print(f"wrote {r.hours_written} hourly rows (UTC)")
    print(f"  first: {r.first_utc.isoformat()}")
    print(f"  last:  {r.last_utc.isoformat()}")
    print(f"repeated local hours resolved: {r.repeated_local_hours_resolved}")
    print(f"price range {r.min_eur_mwh:.2f} .. {r.max_eur_mwh:.2f} EUR/MWh")
    print(f"negative-price hours: {r.negative_price_hours}")


if __name__ == "__main__":
    main()
