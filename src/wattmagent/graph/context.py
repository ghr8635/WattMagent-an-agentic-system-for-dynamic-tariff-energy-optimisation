"""What the agent knows about the household, and how a request becomes planner inputs."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, tzinfo

from wattmagent.data.load import synthetic_load_kw
from wattmagent.data.prices import PricePoint
from wattmagent.devices.battery import BatterySpec
from wattmagent.devices.ev import EVSpec
from wattmagent.graph.validate import HORIZON_HOURS, Validated
from wattmagent.planning.household import HouseholdInputs


@dataclass(frozen=True, slots=True)
class AgentContext:
    prices: Sequence[PricePoint]  # as fetched; timezone-aware
    fetched_at: datetime  # when they were fetched (timezone-aware)
    tz: tzinfo
    ev: EVSpec = EVSpec()
    battery: BatterySpec | None = BatterySpec()
    daily_load_kwh: float = 9.0  # SYNTHETIC base load
    price_basis: str = "wholesale price only, no fees or taxes"


def window_start_for(now: datetime) -> datetime:
    """The plan begins at the next full hour."""
    return now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)


def build_inputs(
    ctx: AgentContext, req: Validated, window_start: datetime
) -> tuple[HouseholdInputs, list[datetime]]:
    """Planner inputs for the next 24 hours, plus the start instant of every slot."""
    by_start = {p.start: p.eur_per_kwh for p in ctx.prices}
    starts = [window_start + timedelta(hours=i) for i in range(HORIZON_HOURS)]
    # Missing hours get a placeholder; the safety layer's freshness check forces the
    # fallback and the response says that prices were incomplete.
    prices = [by_start.get(s, 0.0) for s in starts]
    local_hours = [s.astimezone(ctx.tz).hour for s in starts]
    lo = req.earliest_at or window_start
    plugged = [lo <= s and s + timedelta(hours=1) <= req.depart_at for s in starts]
    ev = replace(
        ctx.ev,
        arrival_soc=req.current_soc,
        target_soc=req.target_soc,
        plug_in_hour=lo.astimezone(ctx.tz).hour,
        depart_hour=req.depart_at.astimezone(ctx.tz).hour,
    )
    inputs = HouseholdInputs(
        prices=prices,
        load_kw=synthetic_load_kw(local_hours, ctx.daily_load_kwh),
        ev_available=plugged,
        ev=ev,
        battery=ctx.battery,
    )
    return inputs, starts
