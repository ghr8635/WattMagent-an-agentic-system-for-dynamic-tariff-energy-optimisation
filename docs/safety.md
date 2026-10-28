# Safety layer

Rule: **nothing reaches a device unless it came out of `plan_with_safety`.** The agent and
the API will call this function and nothing else.

```
prices fresh?  --no-->  fallback
     | yes
optimiser runs --error or infeasible-->  fallback
     | ok
verifier       --rejected-->  fallback
     | passed
   use the optimiser plan
```
The fallback is "charge the EV as early as possible, battery idle" (the `ev_immediately`
rule). It is verified for hard limits too. If even that fails, `PlanRejected` is raised and
nothing is executed. If the EV target is physically out of reach, the result says so
(`request_satisfied = False`) and the best-effort plan is still verified.

## Verifier rules (`safety/verifier.py`)
| Rule | Meaning |
|---|---|
| `no_schedule`, `shape`, `not_finite` | plan missing, wrong length, NaN or infinity |
| `ev_power`, `ev_unplugged` | EV outside 0..charger limit, or charging while not plugged in |
| `ev_target` | EV does not reach the target by departure |
| `ev_not_accepted`, `battery_not_executable` | the simulator cannot do what the plan asks (car full, battery empty or at its reserve, discharge larger than the house demand) |
| `no_battery`, `battery_power` | battery power without a battery, or above its limit |
| `battery_end` | battery ends lower than it started |
| `cost_vs_baseline` | costs more than charging immediately (allowance: 0.1% + 0.5 cent for solver tolerance) |

The verifier uses the device simulators and its own arithmetic, never the optimiser's
internals. A test checks that its cost equals the backtest simulator's bill.

Charging beyond the target is not a safety violation (the car accepts it); it is wasteful,
so the cost rule catches it.

## Price freshness (`safety/freshness.py`)
Prices are rejected if: none; fetched more than 36 h ago; fetched "in the future"
(clock problem); any hour of the planning window is missing; any value is not finite or
beyond +/-3 EUR/kWh (typical unit mistake: EUR/MWh typed as EUR/kWh). Timestamps must be
timezone-aware.

## Audit log (`safety/audit.py`)
One JSON object per call: schema version, request id, time, decision (`optimised` or
`fallback`), outcome (`executed` or `rejected`), reasons, a summary of inputs with a
fingerprint (hash of the inputs, not the inputs), plan cost, verifier result. Numbers and
rule names only; no free text, names or addresses. `JsonlAuditLog` appends one line per
decision. Keep log files out of git (`*.jsonl`).

## Try it
```
python -m wattmagent.backtest --csv data/prices_hourly_utc.csv --household \
    --audit-log audit.jsonl
```
prints `# safety: N optimiser plans verified, F fallbacks (reasons)`.

## Not covered yet
- Idempotency (repeating a request must not double-book a device): arrives with the API.
- The LLM and API failure cases: arrive with the agent and API.
- Execution itself (device interface, advisory mode).
