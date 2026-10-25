from __future__ import annotations

import tempfile
import tomllib
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from wattmagent.backtest.runner import run_backtest
from wattmagent.data.prices import PricePoint
from wattmagent.devices.ev import EVSpec
from wattmagent.planning.baselines import BASELINES
from wattmagent.tariff.tariff import (
    TariffError,
    apply_tariff,
    load_tariff,
    parse_tariff,
)

AS_OF = date(2026, 10, 22)
BERLIN = ZoneInfo("Europe/Berlin")
URL = "https://example.org/preisblatt"

BASE = f"""
name = "Test tariff"
illustrative = false
vat_percent = 19.0
vat_source = "{URL}"
vat_retrieved_on = 2026-10-01

[[components]]
name = "Fixed"
kind = "fixed_per_kwh"
ct_per_kwh = 5.0
source = "{URL}"
retrieved_on = 2026-10-01
"""


def parse(text: str, **kw):
    return parse_tariff(tomllib.loads(text), as_of=AS_OF, **kw)


def rejects(text: str, **kw) -> bool:
    try:
        parse(text, **kw)
    except TariffError:
        return True
    return False


def test_fixed_component_and_vat_formula() -> None:
    t = parse(BASE)
    # (0.10 spot + 0.05 fixed) * 1.19
    assert abs(t.all_in_eur_per_kwh(0.10, 12) - 0.15 * 1.19) < 1e-12


def test_percent_of_spot_component() -> None:
    text = (
        BASE
        + f"""
[[components]]
name = "Margin"
kind = "percent_of_spot"
percent = 10.0
source = "{URL}"
retrieved_on = 2026-10-01
"""
    )
    t = parse(text)
    assert abs(t.all_in_eur_per_kwh(0.20, 0) - (0.20 + 0.05 + 0.02) * 1.19) < 1e-12


def test_negative_spot_with_fixed_fees_can_stay_positive() -> None:
    t = parse(BASE)
    assert t.all_in_eur_per_kwh(-0.02, 3) > 0  # fees outweigh a mildly negative spot price


TOU = (
    BASE
    + f"""
[[components]]
name = "Time-variable grid fee"
kind = "time_of_use"
source = "{URL}"
retrieved_on = 2026-10-01
periods = [
  {{ from_hour = 0, to_hour = 6, ct_per_kwh = 2.0 }},
  {{ from_hour = 6, to_hour = 22, ct_per_kwh = 8.0 }},
  {{ from_hour = 22, to_hour = 24, ct_per_kwh = 2.0 }},
]
"""
)


def test_time_of_use_follows_local_clock_across_dst() -> None:
    t = parse(TOU)
    # 23:00 local is cheap in both winter (UTC+1) and summer (UTC+2).
    winter = datetime(2024, 1, 15, 22, tzinfo=UTC)  # 23:00 CET
    summer = datetime(2024, 7, 15, 21, tzinfo=UTC)  # 23:00 CEST
    pts = apply_tariff([PricePoint(winter, 0.1), PricePoint(summer, 0.1)], t, BERLIN)
    assert abs(pts[0].eur_per_kwh - pts[1].eur_per_kwh) < 1e-12
    noon = apply_tariff([PricePoint(datetime(2024, 1, 15, 11, tzinfo=UTC), 0.1)], t, BERLIN)
    assert noon[0].eur_per_kwh > pts[0].eur_per_kwh  # 12:00 local costs more than 23:00


def test_apply_tariff_without_tz_uses_timestamp_hours() -> None:
    t = parse(TOU)
    pts = apply_tariff([PricePoint(datetime(2024, 1, 15, 12), 0.1)], t)
    assert abs(pts[0].eur_per_kwh - (0.10 + 0.05 + 0.08) * 1.19) < 1e-12


def test_time_of_use_gaps_and_overlaps_are_rejected() -> None:
    gap = TOU.replace("from_hour = 6, to_hour = 22", "from_hour = 7, to_hour = 22")
    overlap = TOU.replace("from_hour = 6, to_hour = 22", "from_hour = 5, to_hour = 22")
    short = TOU.replace("from_hour = 22, to_hour = 24", "from_hour = 22, to_hour = 23")
    assert rejects(gap) and rejects(overlap) and rejects(short)


def test_missing_source_or_date_is_rejected() -> None:
    component_tail = f'source = "{URL}"\nretrieved_on = 2026-10-01\n'
    assert component_tail in BASE
    assert rejects(BASE.replace(component_tail, "retrieved_on = 2026-10-01\n"))  # no source
    assert rejects(BASE.replace(component_tail, f'source = "{URL}"\n'))  # no date
    assert rejects(BASE.replace(f'vat_source = "{URL}"\n', ""))  # no VAT source
    assert rejects(BASE.replace("vat_retrieved_on = 2026-10-01\n", ""))  # no VAT date


def test_real_tariff_needs_real_source() -> None:
    head = f'source = "{URL}"\nretrieved_on'
    assert rejects(BASE.replace(head, 'source = "my notes"\nretrieved_on'))
    ok = BASE.replace(head, 'source = "document:preisblatt.pdf"\nretrieved_on')
    assert not rejects(ok)


def test_illustrative_tariff_may_use_placeholder_sources() -> None:
    text = BASE.replace("illustrative = false", "illustrative = true")
    text = text.replace(URL, "ILLUSTRATIVE: placeholder")
    t = parse(text)
    assert t.illustrative


def test_stale_future_and_implausible_values_are_rejected() -> None:
    stale = BASE.replace("2026-10-01", "2024-01-01")
    assert rejects(stale)
    assert not rejects(stale, max_age_days=2000)  # limit is explicit and adjustable
    assert rejects(BASE.replace("2026-10-01", "2027-01-01"))  # retrieved in the future
    assert rejects(BASE.replace("ct_per_kwh = 5.0", "ct_per_kwh = 500.0"))  # unit mistake
    assert rejects(BASE.replace("vat_percent = 19.0", "vat_percent = 190.0"))
    assert rejects(BASE.replace('kind = "fixed_per_kwh"', 'kind = "mystery"'))
    assert rejects(BASE.replace("ct_per_kwh = 5.0", 'ct_per_kwh = "5"'))


def test_bad_toml_and_empty_components_are_rejected() -> None:
    d = Path(tempfile.mkdtemp())
    bad = d / "bad.toml"
    bad.write_text("name = [unterminated", encoding="utf-8")
    try:
        load_tariff(bad, as_of=AS_OF)
        raise AssertionError("expected TariffError")
    except TariffError:
        pass
    no_components = BASE.split("[[components]]")[0]
    assert rejects(no_components)


def test_shipped_example_tariff_loads_and_is_flagged_illustrative() -> None:
    root = Path(__file__).resolve().parents[2]
    t = load_tariff(root / "config" / "tariffs" / "illustrative_de_household.toml", as_of=AS_OF)
    assert t.illustrative and len(t.components) == 5
    # components: 2.05 + 1.67 + 2.66 + 10.96 + 2.00 = 19.34 ct/kWh; then 19% VAT
    assert abs(t.all_in_eur_per_kwh(0.10, 12) - (0.10 + 0.1934) * 1.19) < 1e-9


def test_time_variable_fee_creates_savings_even_on_flat_spot_prices() -> None:
    t = parse(TOU)
    start = datetime(2024, 1, 10, 0, tzinfo=UTC)
    spot = [PricePoint(start + timedelta(hours=i), 0.10) for i in range(24 * 10)]
    prices = apply_tariff(spot, t, BERLIN)
    res = {r.name: r for r in run_backtest(prices, EVSpec(), BASELINES, tz=BERLIN)}
    imm = res["charge_immediately"].total_cost_eur
    best = res["cheapest_hours (hindsight bound)"].total_cost_eur
    assert best < imm  # the schedule moves charging into the cheap-fee hours
