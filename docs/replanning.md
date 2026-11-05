# Replanning

## The problem

A plan is made at, say, 19:00 for a car that is assumed to be at 30%. During the night
reality drifts: the car was really at 45%, the charger delivered less than planned, an
hour has passed, or fresher prices arrived. Replanning plans again from *now* so the rest
of the night uses the facts that are known now.

## How a client uses it

1. `POST /v1/plans` as before. A plan the agent could make carries a `plan_id` and
   `version: 1`. Requests that only produced questions or an error have no `plan_id`.
2. Later, `POST /v1/plans/{plan_id}/replan` with what the client knows now:

   ```json
   {"current_percent": 55, "battery_percent": 40}
   ```

   `current_percent` (0-100) is the car's real charge and is required. `battery_percent`
   is optional; without it the model's default battery start is used. It must lie between
   the battery's reserve (10%) and its maximum, otherwise the answer is 422.
3. The answer has the same shape as a plan plus `version` (counting up), `previous`
   (the version, charge windows and cost it replaced) and `changed` (whether the charge
   windows differ). Send an `Idempotency-Key` to make retries safe: a repeat returns the
   stored answer and does not plan again.

## What a replan reuses and what it changes

| Kept from the first plan | Taken fresh |
|---|---|
| target charge, departure time, the checked request | the car's charge, from the client |
| (a "not before" time is kept only while it is still in the future) | the time left: planning starts at the next full hour |
| | prices: whatever the price source holds now |
| | optionally the battery's charge |

No text is parsed again. The replan runs from the request that was validated the first time,
so new free text cannot change the target or the departure, and the whole safety pipeline
(freshness check, verifier, fallback, audit record) runs again on the new plan.

## Answers other than a plan

| Case | Answer |
|---|---|
| plan id unknown, expired, malformed, or made by another API key | 404, the same for all four, so ids reveal nothing |
| less than one full hour left before departure | 409: nothing can be planned |
| battery charge outside the battery's limits, or a battery given when none is configured | 422 |
| body invalid (charge outside 0-100, unknown field) | 422 |
| car already at or above the target | 200, a plan with no charging ("No charging is needed") |
| target cannot be reached in the time left | 200, the same fallback and explanation as a first plan |

## Audit trail

The replan's audit record has the request id `<plan_id>.r<version>`, for example
`3f9c....r2`, so a plan and all its replans can be found together. The HTTP `X-Request-ID`
of the replan call is a separate id, in the logs and the response.

## Limits

- **The client has to ask.** Nothing watches the car or the clock and replans by itself;
  a scheduler or the charger's software has to call the endpoint. Triggering replans
  automatically (for example when prices change or the charge drifts) is not built.
- **No devices are controlled.** The answer is a plan to follow, not a command.
- **The reported charge is trusted within 0-100%.** The API cannot check it against the car.
- **Plans live in memory** for `WATTMAGENT_PLAN_TTL_S` (default 36 h) in one process: a restart
  forgets them, and a second replica would not know them. A replan of a forgotten plan is a 404;
  the client can make a new plan.
- **Energy already delivered is not modelled:** the client reports the charge it sees now.
- Base load is still synthetic, and the cost figures use wholesale prices only.
- Replanning uses the built-in runner only; the LangGraph engine is not part of it.
