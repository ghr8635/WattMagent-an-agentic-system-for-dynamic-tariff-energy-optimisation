"""Appliance specs, the planning-day input and the plan.

Time is hourly slots, ``0 .. n-1``. A cycle that starts in slot ``s`` and has a profile of
``L`` hourly power values occupies slots ``s .. s+L-1``. Hourly resolution is coarse for a
two-hour wash; profiles are hourly averages and that is a stated limit.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field

from wattmagent.planning.household import PlanStatus

# Specific heat of water: 4.186 kJ/(kg K) expressed in kWh per litre and kelvin.
KWH_PER_LITRE_K = 4.186 / 3600.0


@dataclass(frozen=True, slots=True)
class CycleSpec:
    """A program that runs once with a fixed power profile (kW per hourly slot)."""

    name: str
    profile_kw: tuple[float, ...]
    description: str = ""

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("a cycle needs a name")
        if not self.profile_kw:
            raise ValueError(f"{self.name}: profile is empty")
        if not all(math.isfinite(v) and v >= 0 for v in self.profile_kw):
            raise ValueError(f"{self.name}: profile values must be finite and not negative")
        if not any(v > 0 for v in self.profile_kw):
            raise ValueError(f"{self.name}: profile never draws power")

    @property
    def duration(self) -> int:
        return len(self.profile_kw)

    @property
    def energy_kwh(self) -> float:
        return math.fsum(self.profile_kw)


@dataclass(frozen=True, slots=True)
class CycleJob:
    """Run ``spec`` once, starting no earlier than ``earliest`` and ending by ``deadline``.

    ``deadline`` is the first slot the cycle may no longer occupy (exclusive end).
    """

    spec: CycleSpec
    earliest: int
    deadline: int

    def __post_init__(self) -> None:
        if self.earliest < 0:
            raise ValueError(f"{self.spec.name}: earliest cannot be negative")
        if self.earliest + self.spec.duration > self.deadline:
            raise ValueError(
                f"{self.spec.name}: window of {self.deadline - self.earliest} h is shorter "
                f"than the {self.spec.duration} h cycle"
            )

    @property
    def starts(self) -> range:
        return range(self.earliest, self.deadline - self.spec.duration + 1)


@dataclass(frozen=True, slots=True)
class TankSpec:
    """Electric hot-water tank. Temperature is one number (a fully mixed tank)."""

    name: str
    volume_l: float
    power_kw: float
    t_min_c: float  # comfort floor
    t_max_c: float  # ceiling (scalding and scale limit, thermostat cut-out)
    t_initial_c: float
    ambient_c: float = 20.0
    loss_kw_per_k: float = 0.0015  # standby loss per kelvin above ambient
    efficiency: float = 1.0
    modulating: bool = False  # False: the element is either off or at full power

    def __post_init__(self) -> None:
        if self.volume_l <= 0 or self.power_kw <= 0:
            raise ValueError(f"{self.name}: volume and power must be positive")
        if not self.t_min_c < self.t_max_c:
            raise ValueError(f"{self.name}: t_min_c must be below t_max_c")
        if not self.t_min_c <= self.t_initial_c <= self.t_max_c:
            raise ValueError(f"{self.name}: t_initial_c must lie inside the comfort band")
        if self.loss_kw_per_k < 0 or not 0 < self.efficiency <= 1:
            raise ValueError(f"{self.name}: loss must be >= 0 and efficiency in (0, 1]")

    @property
    def heat_capacity_kwh_per_k(self) -> float:
        return self.volume_l * KWH_PER_LITRE_K


def tank_step(spec: TankSpec, temp_c: float, power_kw: float, draw_kwh: float) -> float:
    """Temperature after one hour: the physical source of truth (verifier, baselines, billing)."""
    heat = spec.efficiency * power_kw - spec.loss_kw_per_k * (temp_c - spec.ambient_c) - draw_kwh
    return temp_c + heat / spec.heat_capacity_kwh_per_k


@dataclass(frozen=True, slots=True)
class DayInputs:
    """One planning day. Sequences have one entry per hourly slot."""

    prices: Sequence[float]  # EUR/kWh the planner believes
    jobs: Sequence[CycleJob] = ()
    tank: TankSpec | None = None
    tank_draw_kwh: Sequence[float] = ()  # thermal energy taken out per slot
    base_load_kw: Sequence[float] = ()  # other household load; empty means none
    max_grid_kw: float | None = None  # connection limit for base load plus appliances

    def __post_init__(self) -> None:
        n = len(self.prices)
        if n == 0:
            raise ValueError("a planning day needs at least one slot")
        if not all(math.isfinite(p) for p in self.prices):
            raise ValueError("prices must be finite")
        names = [j.spec.name for j in self.jobs]
        if len(set(names)) != len(names):
            raise ValueError("cycle names must be unique within a day")
        for job in self.jobs:
            if job.deadline > n:
                raise ValueError(f"{job.spec.name}: deadline {job.deadline} is beyond {n} slots")
        if self.tank is not None:
            if len(self.tank_draw_kwh) != n:
                raise ValueError("tank_draw_kwh needs one value per slot")
            if any(not math.isfinite(v) or v < 0 for v in self.tank_draw_kwh):
                raise ValueError("tank draws must be finite and not negative")
        if self.base_load_kw and len(self.base_load_kw) != n:
            raise ValueError("base_load_kw needs one value per slot (or none)")
        if self.max_grid_kw is not None and self.max_grid_kw <= 0:
            raise ValueError("max_grid_kw must be positive")

    @property
    def slots(self) -> int:
        return len(self.prices)

    @property
    def base_kw(self) -> list[float]:
        return list(self.base_load_kw) if self.base_load_kw else [0.0] * self.slots


@dataclass(frozen=True, slots=True)
class AppliancePlan:
    status: PlanStatus
    cycle_kw: dict[str, list[float]] = field(default_factory=dict)  # per job name, per slot
    tank_kw: list[float] = field(default_factory=list)
    message: str = ""
    planned_cost_eur: float | None = None  # appliance energy on the planner's prices

    @property
    def feasible(self) -> bool:
        return self.status is not PlanStatus.INFEASIBLE
