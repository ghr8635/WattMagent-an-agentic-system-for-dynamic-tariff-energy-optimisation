"""Household appliances beyond the EV and battery.

Two behaviour classes so far:

- **shiftable cycles** (washing machine, dryer, dishwasher): a fixed power profile that
  runs once, without pausing, between an earliest start and a deadline;
- **hot-water tank** with a comfort band (electric water heater): the optimiser may heat
  early or late as long as the temperature stays inside the band.

Every number for an appliance is an assumption, not a measurement.
"""
