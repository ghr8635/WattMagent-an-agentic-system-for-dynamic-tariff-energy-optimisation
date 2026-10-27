"""Home battery model: a power-limited, lossy store behind the meter.

Conventions (used by the optimiser and the simulator alike):

- ``kw > 0`` charges, ``kw < 0`` discharges; both are measured on the AC side.
- Charging stores ``charge_efficiency`` of the AC energy; discharging delivers
  ``discharge_efficiency`` of the energy taken from the store.
- The battery may only supply the house (and EV). Exporting to the grid is not
  modelled, so the simulator never discharges more than the current demand.

The numbers in ``BatterySpec`` are assumptions, not a particular product. The wear
cost in particular is a planning parameter, not something that appears on a bill.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class BatterySpec:
    capacity_kwh: float = 10.0
    max_power_kw: float = 5.0
    charge_efficiency: float = 0.95
    discharge_efficiency: float = 0.95
    min_soc: float = 0.10  # reserve the planner may not use
    max_soc: float = 1.00
    initial_soc: float = 0.50  # state at the start of every planning day
    wear_eur_per_kwh: float = 0.03  # assumption: cost of using up cycle life, per kWh delivered

    def __post_init__(self) -> None:
        if self.capacity_kwh <= 0 or self.max_power_kw <= 0:
            raise ValueError("battery capacity and power must be positive")
        for name in ("charge_efficiency", "discharge_efficiency"):
            if not 0.0 < getattr(self, name) <= 1.0:
                raise ValueError(f"{name} must be in (0, 1]")
        if not 0.0 <= self.min_soc < self.max_soc <= 1.0:
            raise ValueError("need 0 <= min_soc < max_soc <= 1")
        if not self.min_soc <= self.initial_soc <= self.max_soc:
            raise ValueError("initial_soc must lie between min_soc and max_soc")
        if self.wear_eur_per_kwh < 0:
            raise ValueError("wear cost cannot be negative")

    @property
    def min_kwh(self) -> float:
        return self.min_soc * self.capacity_kwh

    @property
    def max_kwh(self) -> float:
        return self.max_soc * self.capacity_kwh

    @property
    def initial_kwh(self) -> float:
        return self.initial_soc * self.capacity_kwh


class BatterySession:
    """Mutable battery state. ``step`` is the only way energy moves."""

    def __init__(self, spec: BatterySpec) -> None:
        self.spec = spec
        self.stored_kwh = spec.initial_kwh

    def step(
        self, requested_kw: float, hours: float = 1.0, max_delivery_kwh: float = math.inf
    ) -> float:
        """Apply a request for ``hours``. Returns the AC energy exchanged with the house.

        Positive: drawn from the grid to charge. Negative: delivered to the house.
        Requests are clamped to what the hardware can do.
        """
        s = self.spec
        kw = min(max(requested_kw, -s.max_power_kw), s.max_power_kw)
        if kw >= 0:
            room = max(s.max_kwh - self.stored_kwh, 0.0)
            stored_gain = min(kw * hours * s.charge_efficiency, room)
            self.stored_kwh += stored_gain
            return stored_gain / s.charge_efficiency
        usable = max(self.stored_kwh - s.min_kwh, 0.0)
        delivered = min(-kw * hours, usable * s.discharge_efficiency, max(max_delivery_kwh, 0.0))
        self.stored_kwh -= delivered / s.discharge_efficiency
        return -delivered
