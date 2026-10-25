"""Retail tariff model: turns a spot (wholesale) price into the price a household pays.

Why this exists: spot prices alone overstate percentage savings, because grid fees,
taxes and levies are added on top. Every component must carry its source and the
date it was retrieved, so a euro figure can always be traced back.

Formula (per kWh, for one hour):
    net   = spot + sum(fixed) + sum(percent_of_spot * spot) + time_of_use(local hour)
    gross = net * (1 + VAT)

Not modelled on purpose: monthly base fees (they do not change when you charge) and
anything that depends on the volume or peak of consumption.

Safety rules (everything fails loudly):
  - every component needs a source and a retrieved_on date
  - a tariff that is not marked ``illustrative`` needs a real source (URL or
    ``document:<file>``) and values retrieved within ``max_age_days``
  - implausible values and time-of-use schedules with gaps or overlaps are rejected
"""

from __future__ import annotations

import tomllib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, tzinfo
from pathlib import Path
from typing import Any

from wattmagent.data.prices import PricePoint

MAX_ABS_CT_PER_KWH = 100.0  # 1 EUR/kWh: anything larger is almost certainly a unit mistake
MAX_ABS_PERCENT = 200.0
MAX_VAT_PERCENT = 30.0
DEFAULT_MAX_AGE_DAYS = 370  # statutory values usually change on 1 January


class TariffError(ValueError):
    """Invalid or untrustworthy tariff definition."""


@dataclass(frozen=True, slots=True)
class FixedPerKwh:
    name: str
    ct_per_kwh: float
    source: str
    retrieved_on: date


@dataclass(frozen=True, slots=True)
class PercentOfSpot:
    name: str
    percent: float
    source: str
    retrieved_on: date


@dataclass(frozen=True, slots=True)
class TimeOfUse:
    """Local-clock schedule. ``periods`` are (from_hour, to_hour, ct_per_kwh), to exclusive."""

    name: str
    periods: tuple[tuple[int, int, float], ...]
    source: str
    retrieved_on: date

    def ct_at(self, local_hour: int) -> float:
        for start, end, ct in self.periods:
            if start <= local_hour < end:
                return ct
        raise TariffError(f"{self.name}: no period covers hour {local_hour}")


Component = FixedPerKwh | PercentOfSpot | TimeOfUse


@dataclass(frozen=True, slots=True)
class Tariff:
    name: str
    vat_percent: float
    components: tuple[Component, ...]
    illustrative: bool
    vat_source: str
    vat_retrieved_on: date

    def all_in_eur_per_kwh(self, spot_eur_per_kwh: float, local_hour: int) -> float:
        net = spot_eur_per_kwh
        for c in self.components:
            if isinstance(c, FixedPerKwh):
                net += c.ct_per_kwh / 100.0
            elif isinstance(c, PercentOfSpot):
                net += spot_eur_per_kwh * c.percent / 100.0
            else:
                net += c.ct_at(local_hour) / 100.0
        return net * (1.0 + self.vat_percent / 100.0)


def apply_tariff(
    prices: Sequence[PricePoint], tariff: Tariff, tz: tzinfo | None = None
) -> list[PricePoint]:
    """All-in prices. With ``tz`` (and aware timestamps) schedules use local clock hours."""
    out: list[PricePoint] = []
    for p in prices:
        hour = (p.start.astimezone(tz) if tz is not None else p.start).hour
        out.append(PricePoint(p.start, tariff.all_in_eur_per_kwh(p.eur_per_kwh, hour)))
    return out


# ---------------------------------------------------------------- parsing and validation


def _as_date(value: object, what: str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip())
        except ValueError as e:
            raise TariffError(f"{what}: not an ISO date: {value!r}") from e
    raise TariffError(f"{what}: missing or invalid date")


def _num(d: dict[str, Any], key: str, what: str) -> float:
    v = d.get(key)
    if isinstance(v, bool) or not isinstance(v, int | float):
        raise TariffError(f"{what}: '{key}' must be a number")
    return float(v)


def _text(d: dict[str, Any], key: str, what: str) -> str:
    v = d.get(key)
    if not isinstance(v, str) or not v.strip():
        raise TariffError(f"{what}: '{key}' is required")
    return v.strip()


def _check_provenance(
    what: str,
    source: str,
    retrieved_on: date,
    illustrative: bool,
    as_of: date,
    max_age_days: int,
) -> None:
    if retrieved_on > as_of:
        raise TariffError(f"{what}: retrieved_on {retrieved_on} is in the future")
    if illustrative:
        return
    if not source.startswith(("http://", "https://", "document:")):
        raise TariffError(
            f"{what}: a real tariff needs a source URL or 'document:<file>', got {source!r}"
        )
    age = (as_of - retrieved_on).days
    if age > max_age_days:
        raise TariffError(
            f"{what}: values are {age} days old (limit {max_age_days}); re-check and update"
        )


def _check_periods(what: str, periods: list[tuple[int, int, float]]) -> None:
    ordered = sorted(periods)
    if not ordered or ordered[0][0] != 0 or ordered[-1][1] != 24:
        raise TariffError(f"{what}: periods must cover 0..24 exactly")
    for (_a0, a1, _), (b0, _b1, _) in zip(ordered, ordered[1:], strict=False):
        if a1 != b0:
            raise TariffError(f"{what}: gap or overlap between hour {a1} and {b0}")
    for start, end, _ in ordered:
        if not 0 <= start < end <= 24:
            raise TariffError(f"{what}: invalid period {start}-{end}")


def _parse_component(
    raw: dict[str, Any], illustrative: bool, as_of: date, max_age_days: int
) -> Component:
    name = _text(raw, "name", "component")
    what = f"component '{name}'"
    kind = _text(raw, "kind", what)
    source = _text(raw, "source", what)
    retrieved = _as_date(raw.get("retrieved_on"), f"{what}: retrieved_on")
    _check_provenance(what, source, retrieved, illustrative, as_of, max_age_days)

    if kind == "fixed_per_kwh":
        ct = _num(raw, "ct_per_kwh", what)
        if abs(ct) > MAX_ABS_CT_PER_KWH:
            raise TariffError(f"{what}: {ct} ct/kWh is implausible (unit mistake?)")
        return FixedPerKwh(name, ct, source, retrieved)
    if kind == "percent_of_spot":
        pct = _num(raw, "percent", what)
        if abs(pct) > MAX_ABS_PERCENT:
            raise TariffError(f"{what}: {pct}% is implausible")
        return PercentOfSpot(name, pct, source, retrieved)
    if kind == "time_of_use":
        raw_periods = raw.get("periods")
        if not isinstance(raw_periods, list) or not raw_periods:
            raise TariffError(f"{what}: 'periods' list is required")
        periods: list[tuple[int, int, float]] = []
        for p in raw_periods:
            if not isinstance(p, dict):
                raise TariffError(f"{what}: each period must be a table")
            start = int(_num(p, "from_hour", what))
            end = int(_num(p, "to_hour", what))
            ct = _num(p, "ct_per_kwh", what)
            if abs(ct) > MAX_ABS_CT_PER_KWH:
                raise TariffError(f"{what}: {ct} ct/kWh is implausible (unit mistake?)")
            periods.append((start, end, ct))
        _check_periods(what, periods)
        return TimeOfUse(name, tuple(sorted(periods)), source, retrieved)
    raise TariffError(f"{what}: unknown kind {kind!r}")


def parse_tariff(
    raw: dict[str, Any], as_of: date | None = None, max_age_days: int = DEFAULT_MAX_AGE_DAYS
) -> Tariff:
    as_of = as_of or date.today()
    name = _text(raw, "name", "tariff")
    illustrative = raw.get("illustrative", False)
    if not isinstance(illustrative, bool):
        raise TariffError("tariff: 'illustrative' must be true or false")

    vat = _num(raw, "vat_percent", "tariff")
    if not 0.0 <= vat <= MAX_VAT_PERCENT:
        raise TariffError(f"tariff: VAT {vat}% is implausible")
    vat_source = _text(raw, "vat_source", "tariff")
    vat_date = _as_date(raw.get("vat_retrieved_on"), "tariff: vat_retrieved_on")
    _check_provenance("VAT", vat_source, vat_date, illustrative, as_of, max_age_days)

    raw_components = raw.get("components")
    if not isinstance(raw_components, list) or not raw_components:
        raise TariffError("tariff: at least one component is required")
    components = tuple(
        _parse_component(c, illustrative, as_of, max_age_days)
        for c in raw_components
        if isinstance(c, dict)
    )
    if len(components) != len(raw_components):
        raise TariffError("tariff: every component must be a table")
    return Tariff(name, vat, components, illustrative, vat_source, vat_date)


def load_tariff(
    path: Path, as_of: date | None = None, max_age_days: int = DEFAULT_MAX_AGE_DAYS
) -> Tariff:
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise TariffError(f"{path.name}: not valid TOML: {e}") from e
    return parse_tariff(raw, as_of, max_age_days)
