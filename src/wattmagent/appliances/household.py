"""A household described in a TOML file, and its conversion to a planning day.

    name = "Example flat"
    timezone = "Europe/Berlin"
    max_grid_kw = 11.0              # optional connection limit
    daily_base_load_kwh = 9.0       # optional SYNTHETIC base load (only matters with a limit)

    [[cycle]]
    appliance = "dishwasher"        # a key of the catalogue
    earliest_hour = 8               # local clock hours, 0..24, same planning day
    deadline_hour = 18              # the cycle must be finished by then

    [tank]
    appliance = "water_heater_200l"
    daily_draw_kwh = 4.0            # optional overrides of any catalogue value

    [catalogue.cycle.my_washer]     # optional: add your own appliance
    profile_kw = [1.0, 0.6]

Unknown keys are errors, so a typo cannot silently change a household.
"""

from __future__ import annotations

import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from wattmagent.appliances.catalogue import (
    Catalogue,
    ConfigError,
    CycleModel,
    TankModel,
    default_catalogue,
    parse_catalogue,
    parse_cycle,
    parse_tank,
)
from wattmagent.appliances.models import CycleJob, CycleSpec, DayInputs, TankSpec
from wattmagent.data.load import synthetic_load_kw

_TOP = {"name", "timezone", "max_grid_kw", "daily_base_load_kwh", "cycle", "tank", "catalogue"}
_CYCLE_ENTRY = {"appliance", "name", "earliest_hour", "deadline_hour", "profile_kw", "description"}


@dataclass(frozen=True, slots=True)
class CycleEntry:
    model: CycleModel
    name: str
    earliest_hour: int
    deadline_hour: int


@dataclass(frozen=True, slots=True)
class Household:
    name: str
    timezone: str = "Europe/Berlin"
    max_grid_kw: float | None = None
    daily_base_load_kwh: float = 0.0
    cycles: tuple[CycleEntry, ...] = ()
    tank: TankModel | None = None
    notes: list[str] = field(default_factory=list)


def _hour(where: str, value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 24:
        raise ConfigError(f"{where}: expected a whole hour from 0 to 24")
    return value


def parse_household(raw: Mapping[str, Any], catalogue: Catalogue | None = None) -> Household:
    unknown = sorted(set(raw) - _TOP)
    if unknown:
        raise ConfigError(f"household: unknown key(s) {', '.join(unknown)}")
    cat = parse_catalogue(raw.get("catalogue") or {}, catalogue or default_catalogue())

    tz = str(raw.get("timezone", "Europe/Berlin"))
    try:
        ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ConfigError(f"timezone: unknown time zone {tz!r}") from exc

    limit = raw.get("max_grid_kw")
    if limit is not None and (
        isinstance(limit, bool) or not isinstance(limit, int | float) or limit <= 0
    ):
        raise ConfigError("max_grid_kw: expected a positive number")
    base = raw.get("daily_base_load_kwh", 0.0)
    if isinstance(base, bool) or not isinstance(base, int | float) or base < 0:
        raise ConfigError("daily_base_load_kwh: expected a number >= 0")

    entries: list[CycleEntry] = []
    for i, table in enumerate(raw.get("cycle") or []):
        where = f"cycle[{i}]"
        if not isinstance(table, Mapping):
            raise ConfigError(f"{where}: expected a table")
        unknown = sorted(set(table) - _CYCLE_ENTRY)
        if unknown:
            raise ConfigError(f"{where}: unknown key(s) {', '.join(unknown)}")
        key = table.get("appliance")
        if not isinstance(key, str) or key not in cat.cycles:
            known = ", ".join(sorted(cat.cycles))
            raise ConfigError(f"{where}: unknown appliance {key!r}; known cycles: {known}")
        model = cat.cycles[key]
        if "profile_kw" in table or "description" in table:
            merged = {"description": model.description, "profile_kw": list(model.profile_kw)}
            merged.update({k: table[k] for k in ("profile_kw", "description") if k in table})
            model = parse_cycle(key, merged)
        a = _hour(f"{where}.earliest_hour", table.get("earliest_hour", 0))
        b = _hour(f"{where}.deadline_hour", table.get("deadline_hour", 24))
        if a + len(model.profile_kw) > b:
            raise ConfigError(
                f"{where}: window {a}-{b} h is shorter than the {len(model.profile_kw)} h cycle"
            )
        entries.append(CycleEntry(model, str(table.get("name", key)), a, b))
    names = [e.name for e in entries]
    if len(set(names)) != len(names):
        raise ConfigError("cycle names must be unique; give repeated appliances a 'name'")

    tank: TankModel | None = None
    table = raw.get("tank")
    if table is not None:
        if not isinstance(table, Mapping):
            raise ConfigError("tank: expected a table")
        key = table.get("appliance")
        if not isinstance(key, str) or key not in cat.tanks:
            known = ", ".join(sorted(cat.tanks))
            raise ConfigError(f"tank: unknown appliance {key!r}; known tanks: {known}")
        m = cat.tanks[key]
        merged_t: dict[str, Any] = {
            "description": m.description, "volume_l": m.volume_l, "power_kw": m.power_kw,
            "t_min_c": m.t_min_c, "t_max_c": m.t_max_c, "t_initial_c": m.t_initial_c,
            "ambient_c": m.ambient_c, "loss_kw_per_k": m.loss_kw_per_k,
            "efficiency": m.efficiency, "modulating": m.modulating,
            "daily_draw_kwh": m.daily_draw_kwh, "draw_shape": list(m.draw_shape),
        }  # fmt: skip
        merged_t.update({k: v for k, v in table.items() if k != "appliance"})
        tank = parse_tank(key, merged_t)
        _validate_tank(tank)

    return Household(
        name=str(raw.get("name", "household")),
        timezone=tz,
        max_grid_kw=float(limit) if limit is not None else None,
        daily_base_load_kwh=float(base),
        cycles=tuple(entries),
        tank=tank,
    )


def _validate_tank(m: TankModel) -> None:
    try:
        tank_spec(m)
    except ValueError as exc:
        raise ConfigError(f"tank: {exc}") from exc


def tank_spec(m: TankModel) -> TankSpec:
    return TankSpec(
        name=m.key,
        volume_l=m.volume_l,
        power_kw=m.power_kw,
        t_min_c=m.t_min_c,
        t_max_c=m.t_max_c,
        t_initial_c=m.t_initial_c,
        ambient_c=m.ambient_c,
        loss_kw_per_k=m.loss_kw_per_k,
        efficiency=m.efficiency,
        modulating=m.modulating,
    )


def load_household(path: Path, catalogue: Catalogue | None = None) -> Household:
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path.name}: not valid TOML: {exc}") from exc
    try:
        return parse_household(raw, catalogue)
    except ConfigError as exc:
        raise ConfigError(f"{path.name}: {exc}") from exc


def _window_slots(hours: Sequence[int], earliest: int, deadline: int) -> tuple[int, int]:
    """Slot range for the local clock window [earliest, deadline). Handles 23 and 25 h days."""
    n = len(hours)
    first = next((i for i, h in enumerate(hours) if h >= earliest), n)
    end = n if deadline >= 24 else next((i for i, h in enumerate(hours) if h >= deadline), n)
    return first, end


def build_day_inputs(
    household: Household, prices: Sequence[float], local_hours: Sequence[int]
) -> DayInputs:
    """A planning day for ``household``. ``local_hours[t]`` is slot ``t``'s local clock hour."""
    if len(prices) != len(local_hours):
        raise ValueError("prices and local_hours must have the same length")
    jobs: list[CycleJob] = []
    for entry in household.cycles:
        a, b = _window_slots(local_hours, entry.earliest_hour, entry.deadline_hour)
        spec = CycleSpec(entry.name, entry.model.profile_kw, entry.model.description)
        jobs.append(CycleJob(spec, a, b))

    tank: TankSpec | None = None
    draws: list[float] = []
    if household.tank is not None:
        tank = tank_spec(household.tank)
        shape = household.tank.draw_shape
        total = sum(shape)
        draws = [household.tank.daily_draw_kwh * shape[h % 24] / total for h in local_hours]

    base = (
        synthetic_load_kw(local_hours, household.daily_base_load_kwh)
        if household.daily_base_load_kwh > 0
        else []
    )
    return DayInputs(
        prices=list(prices),
        jobs=jobs,
        tank=tank,
        tank_draw_kwh=draws,
        base_load_kw=base,
        max_grid_kw=household.max_grid_kw,
    )
