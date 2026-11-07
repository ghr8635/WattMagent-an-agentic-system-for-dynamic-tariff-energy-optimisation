"""The appliance catalogue: ready-made appliances read from TOML.

The shipped file is ``catalogue.toml`` next to this module. A household file can add or
replace entries under ``[catalogue.cycle.<key>]`` and ``[catalogue.tank.<key>]``, so adding an
appliance is a config entry, not code.
"""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from importlib import resources
from typing import Any

from wattmagent.core.errors import WattMagentError


class ConfigError(WattMagentError):
    """A catalogue or household file that cannot be used; the message says where."""


@dataclass(frozen=True, slots=True)
class TankModel:
    """A tank's physics plus the household's hot-water use that goes with it."""

    key: str
    description: str
    volume_l: float
    power_kw: float
    t_min_c: float
    t_max_c: float
    t_initial_c: float
    ambient_c: float
    loss_kw_per_k: float
    efficiency: float
    modulating: bool
    daily_draw_kwh: float
    draw_shape: tuple[float, ...]


@dataclass(frozen=True, slots=True)
class CycleModel:
    key: str
    description: str
    profile_kw: tuple[float, ...]


@dataclass(frozen=True, slots=True)
class HeatPumpModel:
    key: str
    description: str
    heat_capacity_kwh_per_k: float
    ua_kw_per_k: float
    max_heat_kw: float
    t_min_c: float
    t_max_c: float
    t_initial_c: float
    cop_at_7c: float
    cop_slope_per_k: float
    cop_min: float
    cop_max: float


@dataclass(frozen=True, slots=True)
class Catalogue:
    cycles: dict[str, CycleModel] = field(default_factory=dict)
    tanks: dict[str, TankModel] = field(default_factory=dict)
    heatpumps: dict[str, HeatPumpModel] = field(default_factory=dict)


_CYCLE_KEYS = {"description", "profile_kw"}
_TANK_REQUIRED = {
    "volume_l",
    "power_kw",
    "t_min_c",
    "t_max_c",
    "t_initial_c",
    "daily_draw_kwh",
    "draw_shape",
}
_TANK_OPTIONAL = {"description", "ambient_c", "loss_kw_per_k", "efficiency", "modulating"}


_HP_REQUIRED = {
    "heat_capacity_kwh_per_k",
    "ua_kw_per_k",
    "max_heat_kw",
    "t_min_c",
    "t_max_c",
    "t_initial_c",
}
_HP_OPTIONAL = {"description", "cop_at_7c", "cop_slope_per_k", "cop_min", "cop_max"}


def _numbers(where: str, value: Any, count: int | None = None) -> tuple[float, ...]:
    if not isinstance(value, list) or not value:
        raise ConfigError(f"{where}: expected a non-empty list of numbers")
    out: list[float] = []
    for v in value:
        if isinstance(v, bool) or not isinstance(v, int | float):
            raise ConfigError(f"{where}: {v!r} is not a number")
        out.append(float(v))
    if count is not None and len(out) != count:
        raise ConfigError(f"{where}: expected {count} values, got {len(out)}")
    return tuple(out)


def _num(where: str, value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ConfigError(f"{where}: {value!r} is not a number")
    return float(value)


def _check_keys(where: str, table: Mapping[str, Any], allowed: set[str]) -> None:
    unknown = sorted(set(table) - allowed)
    if unknown:
        raise ConfigError(f"{where}: unknown key(s) {', '.join(unknown)}")


def parse_cycle(key: str, table: Mapping[str, Any]) -> CycleModel:
    where = f"cycle.{key}"
    _check_keys(where, table, _CYCLE_KEYS)
    if "profile_kw" not in table:
        raise ConfigError(f"{where}: profile_kw is required")
    profile = _numbers(f"{where}.profile_kw", table["profile_kw"])
    if any(v < 0 for v in profile) or not any(v > 0 for v in profile):
        raise ConfigError(f"{where}.profile_kw: values must be >= 0 and not all zero")
    return CycleModel(key, str(table.get("description", "")), profile)


def parse_tank(key: str, table: Mapping[str, Any]) -> TankModel:
    where = f"tank.{key}"
    _check_keys(where, table, _TANK_REQUIRED | _TANK_OPTIONAL)
    missing = sorted(_TANK_REQUIRED - set(table))
    if missing:
        raise ConfigError(f"{where}: missing {', '.join(missing)}")
    shape = _numbers(f"{where}.draw_shape", table["draw_shape"], 24)
    if any(v < 0 for v in shape) or sum(shape) <= 0:
        raise ConfigError(f"{where}.draw_shape: weights must be >= 0 with a positive sum")
    modulating = table.get("modulating", False)
    if not isinstance(modulating, bool):
        raise ConfigError(f"{where}.modulating: expected true or false")
    daily = _num(f"{where}.daily_draw_kwh", table["daily_draw_kwh"])
    if daily < 0:
        raise ConfigError(f"{where}.daily_draw_kwh: cannot be negative")
    return TankModel(
        key=key,
        description=str(table.get("description", "")),
        volume_l=_num(f"{where}.volume_l", table["volume_l"]),
        power_kw=_num(f"{where}.power_kw", table["power_kw"]),
        t_min_c=_num(f"{where}.t_min_c", table["t_min_c"]),
        t_max_c=_num(f"{where}.t_max_c", table["t_max_c"]),
        t_initial_c=_num(f"{where}.t_initial_c", table["t_initial_c"]),
        ambient_c=_num(f"{where}.ambient_c", table.get("ambient_c", 20.0)),
        loss_kw_per_k=_num(f"{where}.loss_kw_per_k", table.get("loss_kw_per_k", 0.0015)),
        efficiency=_num(f"{where}.efficiency", table.get("efficiency", 1.0)),
        modulating=modulating,
        daily_draw_kwh=daily,
        draw_shape=shape,
    )


def parse_heatpump(key: str, table: Mapping[str, Any]) -> HeatPumpModel:
    where = f"heatpump.{key}"
    _check_keys(where, table, _HP_REQUIRED | _HP_OPTIONAL)
    missing = sorted(_HP_REQUIRED - set(table))
    if missing:
        raise ConfigError(f"{where}: missing {', '.join(missing)}")

    def num(name: str, default: float | None = None) -> float:
        return _num(f"{where}.{name}", table.get(name, default))

    return HeatPumpModel(
        key=key,
        description=str(table.get("description", "")),
        heat_capacity_kwh_per_k=num("heat_capacity_kwh_per_k"),
        ua_kw_per_k=num("ua_kw_per_k"),
        max_heat_kw=num("max_heat_kw"),
        t_min_c=num("t_min_c"),
        t_max_c=num("t_max_c"),
        t_initial_c=num("t_initial_c"),
        cop_at_7c=num("cop_at_7c", 3.5),
        cop_slope_per_k=num("cop_slope_per_k", 0.07),
        cop_min=num("cop_min", 1.8),
        cop_max=num("cop_max", 5.0),
    )


def parse_catalogue(raw: Mapping[str, Any], base: Catalogue | None = None) -> Catalogue:
    """Entries in ``raw`` (``{"cycle": {...}, "tank": {...}}``) added to or replacing ``base``."""
    _check_keys("catalogue", raw, {"cycle", "tank", "heatpump"})
    cycles = dict(base.cycles) if base else {}
    tanks = dict(base.tanks) if base else {}
    heatpumps = dict(base.heatpumps) if base else {}
    for key, table in (raw.get("cycle") or {}).items():
        if not isinstance(table, Mapping):
            raise ConfigError(f"cycle.{key}: expected a table")
        cycles[key] = parse_cycle(key, table)
    for key, table in (raw.get("tank") or {}).items():
        if not isinstance(table, Mapping):
            raise ConfigError(f"tank.{key}: expected a table")
        tanks[key] = parse_tank(key, table)
    for key, table in (raw.get("heatpump") or {}).items():
        if not isinstance(table, Mapping):
            raise ConfigError(f"heatpump.{key}: expected a table")
        heatpumps[key] = parse_heatpump(key, table)
    return Catalogue(cycles, tanks, heatpumps)


def default_catalogue() -> Catalogue:
    text = resources.files("wattmagent.appliances").joinpath("catalogue.toml").read_text("utf-8")
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:  # pragma: no cover  (the shipped file is tested)
        raise ConfigError(f"catalogue.toml: not valid TOML: {exc}") from exc
    return parse_catalogue(raw)
