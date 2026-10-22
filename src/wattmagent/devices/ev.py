"""Electric vehicle model: a battery behind a power-limited, lossy charger.

The simulator is the single source of truth for physics. Planners request power;
``EVSession.charge`` clamps it to what the hardware can actually do and returns
the grid energy really drawn, which is what gets billed.
"""

from __future__ import annotations

from dataclasses import dataclass

_EPS = 1e-9


@dataclass(frozen=True, slots=True)
class EVSpec:
    capacity_kwh: float = 60.0
    max_charge_kw: float = 11.0
    efficiency: float = 0.92  # grid energy -> stored energy
    plug_in_hour: int = 18
    depart_hour: int = 7
    arrival_soc: float = 0.30
    target_soc: float = 0.80

    def __post_init__(self) -> None:
        if not (0.0 < self.efficiency <= 1.0):
            raise ValueError("efficiency must be in (0, 1]")
        if self.capacity_kwh <= 0 or self.max_charge_kw <= 0:
            raise ValueError("capacity and charger power must be positive")
        if not (0.0 <= self.arrival_soc <= 1.0 and 0.0 <= self.target_soc <= 1.0):
            raise ValueError("SoC values must be in [0, 1]")
        if not (0 <= self.plug_in_hour < 24 and 0 <= self.depart_hour < 24):
            raise ValueError("hours must be in [0, 24)")

    @property
    def window_hours(self) -> int:
        """Hours between plugging in and departing (wraps past midnight)."""
        return (self.depart_hour - self.plug_in_hour) % 24 or 24

    @property
    def grid_energy_needed_kwh(self) -> float:
        stored = max(0.0, (self.target_soc - self.arrival_soc) * self.capacity_kwh)
        return stored / self.efficiency


class EVSession:
    """One plug-in session. Mutable state: the battery's stored energy."""

    def __init__(self, spec: EVSpec) -> None:
        self.spec = spec
        self.stored_kwh = spec.arrival_soc * spec.capacity_kwh

    @property
    def soc(self) -> float:
        return self.stored_kwh / self.spec.capacity_kwh

    def charge(self, requested_kw: float, hours: float = 1.0) -> float:
        """Charge for ``hours`` at up to ``requested_kw``. Returns grid kWh drawn."""
        kw = min(max(requested_kw, 0.0), self.spec.max_charge_kw)
        room_kwh = self.spec.capacity_kwh - self.stored_kwh
        stored_gain = min(kw * hours * self.spec.efficiency, room_kwh)
        self.stored_kwh += stored_gain
        return stored_gain / self.spec.efficiency

    @property
    def target_met(self) -> bool:
        no_charge_needed = self.spec.target_soc <= self.spec.arrival_soc + _EPS
        return no_charge_needed or self.soc >= self.spec.target_soc - 1e-6
