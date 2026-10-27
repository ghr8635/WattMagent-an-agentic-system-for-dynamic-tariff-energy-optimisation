"""Synthetic household base load. NOT measured data.

Without a base load a home battery has nothing to supply, so the optimiser needs
one. This is a fixed daily shape (low at night, a morning bump, an evening peak)
scaled to a chosen daily energy. A real meter export can replace it later behind
the same function signature.
"""

from __future__ import annotations

from collections.abc import Sequence

# Relative weight per local clock hour, 0..23. Only the shape matters; it is rescaled.
_SHAPE = (
    0.45, 0.40, 0.38, 0.38, 0.40, 0.55,  # 00-05
    0.90, 1.20, 1.10, 0.85, 0.80, 0.85,  # 06-11
    1.00, 0.95, 0.80, 0.80, 0.90, 1.20,  # 12-17
    1.60, 1.70, 1.50, 1.20, 0.90, 0.60,  # 18-23
)  # fmt: skip


def synthetic_load_kw(local_hours: Sequence[int], daily_kwh: float = 9.0) -> list[float]:
    """Average power (kW) for each slot, given each slot's local clock hour.

    Scaled so that 24 consecutive hours add up to ``daily_kwh`` (9 kWh/day is roughly
    a small household; the value is an assumption).
    """
    if daily_kwh < 0:
        raise ValueError("daily_kwh cannot be negative")
    scale = daily_kwh / sum(_SHAPE)
    out: list[float] = []
    for h in local_hours:
        if not 0 <= h < 24:
            raise ValueError(f"hour out of range: {h}")
        out.append(_SHAPE[h] * scale)
    return out
