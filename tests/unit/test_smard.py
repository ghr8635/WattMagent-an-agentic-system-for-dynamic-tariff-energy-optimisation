from __future__ import annotations

import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from wattmagent.data.prices import load_csv
from wattmagent.data.smard import convert_smard_csv, parse_smard_date

BERLIN = ZoneInfo("Europe/Berlin")
MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
COL = "Germany/Luxembourg [€/MWh] Calculated resolutions"
HEADER = f"Start date;End date;{COL};∅ DE/LU neighbours [€/MWh] Calculated resolutions"


def fmt(dt: datetime) -> str:
    h12 = dt.hour % 12 or 12
    ampm = "AM" if dt.hour < 12 else "PM"
    return f"{MON[dt.month - 1]} {dt.day}, {dt.year} {h12}:{dt.minute:02d} {ampm}"


def smard_text(start_utc: datetime, n: int, step_min: int = 60, price=lambda i: float(i)) -> str:
    """Build SMARD-style text (local-time stamps) for n steps starting at start_utc."""
    lines = [HEADER]
    for i in range(n):
        a = start_utc + timedelta(minutes=step_min * i)
        b = a + timedelta(minutes=step_min)
        la = a.astimezone(BERLIN).replace(tzinfo=None)
        lb = b.astimezone(BERLIN).replace(tzinfo=None)
        lines.append(f"{fmt(la)};{fmt(lb)};{price(i):.2f};99.00")
    return "\n".join(lines) + "\n"


def convert(text: str, **kw):
    d = Path(tempfile.mkdtemp())
    src, dst = d / "in.csv", d / "out.csv"
    src.write_text(text, encoding="utf-8")
    report = convert_smard_csv(src, dst, **kw)
    return report, dst


def raises(fn) -> bool:
    try:
        fn()
    except ValueError:
        return True
    return False


def test_parse_dates_incl_noon_and_midnight() -> None:
    assert parse_smard_date("Jan 1, 2024 12:00 AM") == datetime(2024, 1, 1, 0, 0)
    assert parse_smard_date("Jan 1, 2024 12:00 PM") == datetime(2024, 1, 1, 12, 0)
    assert parse_smard_date("Dec 31, 2024 11:45 PM") == datetime(2024, 12, 31, 23, 45)
    assert raises(lambda: parse_smard_date("1.1.2024 00:00"))


def test_roundtrip_across_both_dst_changes_gives_regular_utc_hours() -> None:
    start = datetime(2024, 3, 30, 0, tzinfo=UTC)  # covers 31 March (23 local hours)
    n = 24 * 10
    report, dst = convert(smard_text(start, n))
    assert report.hours_written == n and report.resolution_minutes == 60
    prices = load_csv(dst)  # the real loader must accept our output
    assert [p.start for p in prices] == [start + timedelta(hours=i) for i in range(n)]
    assert [round(p.eur_per_kwh * 1000) for p in prices] == list(range(n))


def test_october_repeated_hour_is_resolved_in_order() -> None:
    start = datetime(2024, 10, 26, 12, tzinfo=UTC)  # covers 27 Oct (25 local hours)
    n = 24 * 3
    report, dst = convert(smard_text(start, n))
    assert report.repeated_local_hours_resolved == 1
    prices = load_csv(dst)
    assert [p.start for p in prices] == [start + timedelta(hours=i) for i in range(n)]


def test_quarter_hourly_file_is_averaged_to_hours() -> None:
    start = datetime(2025, 10, 1, 0, tzinfo=UTC)

    def price(i: int) -> float:
        return [10, 20, 30, 40][i % 4] + 100 * (i // 4)

    report, dst = convert(smard_text(start, 8, step_min=15, price=price))
    assert report.resolution_minutes == 15 and report.hours_written == 2
    prices = load_csv(dst)
    assert [round(p.eur_per_kwh * 1000, 3) for p in prices] == [25.0, 125.0]


def test_missing_price_fails_loudly() -> None:
    text = smard_text(datetime(2024, 1, 1, tzinfo=UTC), 5).replace(";2.00;", ";-;")
    assert raises(lambda: convert(text))


def test_gap_in_series_fails_loudly() -> None:
    lines = smard_text(datetime(2024, 1, 1, tzinfo=UTC), 6).splitlines()
    del lines[3]
    assert raises(lambda: convert("\n".join(lines) + "\n"))


def test_utc_file_misread_as_berlin_is_rejected() -> None:
    # Same wall-clock text, but the file is really UTC: converting as Berlin must fail
    # on the spring-forward day instead of silently shifting prices by an hour.
    lines = [HEADER]
    t = datetime(2024, 3, 30, 12)
    for i in range(48):
        a, b = t + timedelta(hours=i), t + timedelta(hours=i + 1)
        lines.append(f"{fmt(a)};{fmt(b)};{i:.2f};99.00")
    text = "\n".join(lines) + "\n"
    assert raises(lambda: convert(text))
    report, _ = convert(text, tz_name="UTC")
    assert report.hours_written == 48


def test_wrong_column_and_bad_header() -> None:
    text = smard_text(datetime(2024, 1, 1, tzinfo=UTC), 3)
    assert raises(lambda: convert(text, column_prefix="France"))
    assert raises(lambda: convert("Datum von;Datum bis;Preis\n1;2;3\n"))
