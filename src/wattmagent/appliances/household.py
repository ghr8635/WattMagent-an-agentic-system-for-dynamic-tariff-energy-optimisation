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

    [heatpump]
    appliance = "heat_pump_efficient_house"   # needs outdoor temperatures to be planned

    [catalogue.cycle.my_washer]     # optional: add your own appliance
    profile_kw = [1.0, 0.6]

Unknown keys are errors, so a typo cannot silently change a household.
"""

from __future__ import annotations

import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from wattmagent.appliances.catalogue import (
    Catalogue,
    ConfigError,
    CycleModel,
    HeatPumpModel,
    TankModel,
    default_catalogue,
    parse_catalogue,
    parse_cycle,
    parse_heatpump,
    parse_tank,
)
from wattmagent.appliances.models import BuildingSpec, CycleJob, CycleSpec, DayInputs, TankSpec
from wattmagent.data.load import synthetic_load_kw

_TOP = {
    "name",
    "timezone",
    "max_grid_kw",
    "daily_base_load_kwh",
    "cycle",
    "tank",
    "heatpump",
    "solar",
    "catalogue",
}
_SOLAR_KEYS = {"kwp", "performance_ratio", "export_price_eur_kwh"}
_CYCLE_ENTRY = {"appliance", "name", "earliest_hour", "deadline_hour", "profile_kw", "description"}


@dataclass(frozen=True, slots=True)
class SolarModel:
    """Rooftop PV: generation = kwp x (radiation / 1000 W/m2) x performance_ratio.

    The performance ratio folds in tilt and orientation, temperature, inverter and cable
    losses; 0.8 is a round assumption, not a measurement of any roof.
    """

    kwp: float
    performance_ratio: float = 0.8
    export_price_eur_kwh: float = 0.08  # ASSUMPTION: check the current feed-in rate


def pv_power_kw(solar: SolarModel, radiation_w_m2: float) -> float:
    return solar.kwp * (radiation_w_m2 / 1000.0) * solar.performance_ratio


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
    heatpump: HeatPumpModel | None = None
    solar: SolarModel | None = None
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

    heatpump: HeatPumpModel | None = None
    hp_table = raw.get("heatpump")
    if hp_table is not None:
        if not isinstance(hp_table, Mapping):
            raise ConfigError("heatpump: expected a table")
        hp_key = hp_table.get("appliance")
        if not isinstance(hp_key, str) or hp_key not in cat.heatpumps:
            known = ", ".join(sorted(cat.heatpumps))
            raise ConfigError(f"heatpump: unknown appliance {hp_key!r}; known heat pumps: {known}")
        hp = cat.heatpumps[hp_key]
        merged_h: dict[str, Any] = {k: v for k, v in asdict(hp).items() if k != "key"}
        merged_h.update({k: v for k, v in hp_table.items() if k != "appliance"})
        heatpump = parse_heatpump(hp_key, merged_h)
        try:
            building_spec(heatpump)
        except ValueError as exc:
            raise ConfigError(f"heatpump: {exc}") from exc

    solar: SolarModel | None = None
    sol_table = raw.get("solar")
    if sol_table is not None:
        if not isinstance(sol_table, Mapping):
            raise ConfigError("solar: expected a table")
        unknown = sorted(set(sol_table) - _SOLAR_KEYS)
        if unknown:
            raise ConfigError(f"solar: unknown key(s) {', '.join(unknown)}")
        if "kwp" not in sol_table:
            raise ConfigError("solar: kwp is required")
        values = {k: sol_table[k] for k in _SOLAR_KEYS if k in sol_table}
        for k, v in values.items():
            if isinstance(v, bool) or not isinstance(v, int | float):
                raise ConfigError(f"solar.{k}: {v!r} is not a number")
        kwp = float(values["kwp"])
        ratio = float(values.get("performance_ratio", 0.8))
        export = float(values.get("export_price_eur_kwh", 0.08))
        if not 0 < kwp <= 100:
            raise ConfigError("solar.kwp: expected a number above 0 and up to 100")
        if not 0 < ratio <= 1:
            raise ConfigError("solar.performance_ratio: expected a number in (0, 1]")
        if export < 0:
            raise ConfigError("solar.export_price_eur_kwh: cannot be negative")
        solar = SolarModel(kwp, ratio, export)

    return Household(
        name=str(raw.get("name", "household")),
        timezone=tz,
        max_grid_kw=float(limit) if limit is not None else None,
        daily_base_load_kwh=float(base),
        cycles=tuple(entries),
        tank=tank,
        heatpump=heatpump,
        solar=solar,
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


def building_spec(m: HeatPumpModel) -> BuildingSpec:
    return BuildingSpec(
        name=m.key,
        heat_capacity_kwh_per_k=m.heat_capacity_kwh_per_k,
        ua_kw_per_k=m.ua_kw_per_k,
        max_heat_kw=m.max_heat_kw,
        t_min_c=m.t_min_c,
        t_max_c=m.t_max_c,
        t_initial_c=m.t_initial_c,
        cop_at_7c=m.cop_at_7c,
        cop_slope_per_k=m.cop_slope_per_k,
        cop_min=m.cop_min,
        cop_max=m.cop_max,
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
    household: Household,
    prices: Sequence[float],
    local_hours: Sequence[int],
    outdoor_c: Sequence[float] | None = None,
    radiation_w_m2: Sequence[float] | None = None,
) -> DayInputs:
    """A planning day for ``household``. ``local_hours[t]`` is slot ``t``'s local clock hour.

    The heat pump is part of the day only when ``outdoor_c`` (one temperature per slot) is
    given: without weather there is nothing to plan it against. Pass ``None`` for a day
    with no heating need. Solar is part of the day only when ``radiation_w_m2`` is given.
    """
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
    building: BuildingSpec | None = None
    if household.heatpump is not None and outdoor_c is not None:
        if len(outdoor_c) != len(prices):
            raise ValueError("outdoor_c and prices must have the same length")
        building = building_spec(household.heatpump)
    pv: list[float] = []
    if household.solar is not None and radiation_w_m2 is not None:
        if len(radiation_w_m2) != len(prices):
            raise ValueError("radiation_w_m2 and prices must have the same length")
        pv = [pv_power_kw(household.solar, g) for g in radiation_w_m2]
    return DayInputs(
        prices=list(prices),
        jobs=jobs,
        tank=tank,
        tank_draw_kwh=draws,
        base_load_kw=base,
        max_grid_kw=household.max_grid_kw,
        pv_kw=pv,
        export_price_eur_kwh=household.solar.export_price_eur_kwh
        if pv and household.solar
        else 0.0,
        building=building,
        outdoor_c=list(outdoor_c) if building is not None and outdoor_c is not None else (),
    )
