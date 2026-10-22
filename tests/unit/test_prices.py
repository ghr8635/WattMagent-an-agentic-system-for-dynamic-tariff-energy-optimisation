from __future__ import annotations

import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from wattmagent.data.prices import load_csv, synthetic_prices


def write_csv(text: str) -> Path:
    d = tempfile.mkdtemp()
    p = Path(d) / "prices.csv"
    p.write_text(text, encoding="utf-8")
    return p


def raises_value_error(fn) -> bool:  # tiny helper so tests run without pytest too
    try:
        fn()
    except ValueError:
        return True
    return False


def test_synthetic_is_deterministic_and_hourly() -> None:
    a = synthetic_prices(datetime(2025, 1, 1), 3, seed=7)
    b = synthetic_prices(datetime(2025, 1, 1), 3, seed=7)
    c = synthetic_prices(datetime(2025, 1, 1), 3, seed=8)
    assert a == b and a != c
    assert len(a) == 72
    assert all(y.start - x.start == timedelta(hours=1) for x, y in zip(a, a[1:], strict=False))


def test_synthetic_rejects_bad_days() -> None:
    assert raises_value_error(lambda: synthetic_prices(datetime(2025, 1, 1), 0, seed=1))


def test_csv_hourly_converts_units() -> None:
    p = write_csv(
        "timestamp,price_eur_mwh\n2025-01-01T00:00:00,100\n2025-01-01T01:00:00,50\n"
    )
    pts = load_csv(p)
    assert [round(x.eur_per_kwh, 6) for x in pts] == [0.1, 0.05]


def test_csv_quarter_hourly_is_averaged_to_hours() -> None:
    rows = ["timestamp,price_eur_mwh"]
    for q, price in enumerate([100, 200, 300, 400]):
        rows.append(f"2025-01-01T00:{q * 15:02d}:00,{price}")
    for q in range(4):
        rows.append(f"2025-01-01T01:{q * 15:02d}:00,80")
    pts = load_csv(write_csv("\n".join(rows) + "\n"))
    assert len(pts) == 2
    assert round(pts[0].eur_per_kwh, 6) == 0.25  # mean(100..400)=250 EUR/MWh
    assert round(pts[1].eur_per_kwh, 6) == 0.08


def test_csv_partial_hour_fails_loudly() -> None:
    rows = ["timestamp,price_eur_mwh"]
    rows += [f"2025-01-01T00:{q * 15:02d}:00,100" for q in range(4)]
    rows += ["2025-01-01T01:00:00,100", "2025-01-01T01:15:00,100"]  # only 2 of 4
    assert raises_value_error(lambda: load_csv(write_csv("\n".join(rows) + "\n")))


def test_csv_gap_fails_loudly() -> None:
    p = write_csv("timestamp,price_eur_mwh\n2025-01-01T00:00:00,1\n2025-01-01T02:00:00,1\n")
    assert raises_value_error(lambda: load_csv(p))


def test_csv_missing_columns_and_empty() -> None:
    assert raises_value_error(lambda: load_csv(write_csv("a,b\n1,2\n")))
    assert raises_value_error(lambda: load_csv(write_csv("timestamp,price_eur_mwh\n")))
