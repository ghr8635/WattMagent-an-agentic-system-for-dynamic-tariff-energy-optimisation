# Web page

Served by the API itself at `/ui`. Plain HTML, CSS and JavaScript in `src/wattmagent/api/ui/`;
no framework, build step, CDN or web font.

## What it shows

- **Request box:** sends the text to `POST /v1/plans` with a fresh `Idempotency-Key` and shows the
  answer. The page then reloads the plan and the decision log.
- **The day:** the decision (optimised or safe fallback), the planned bill, the bill if
  everything ran at once, and the difference. The difference is a model figure, not a savings
  claim: appliance figures are assumptions and the base load is a fixed profile.
- **Price chart:** wholesale day-ahead price per hour; hours in which the plan uses energy are
  shaded; hours already past are faded and a dashed line marks "now".
- **Timeline:** one row per appliance, the hot water tank and the battery.
- **Energy by hour:** planned consumption, stacked bars. **Planned, not measured.** With Home
  Assistant history connected (below), measured hourly energy is drawn as dots over the bars.
- **Battery and tank:** simulated stored energy and tank temperature.
- **Recent decisions:** from the audit log (time, kind, decision, verifier result, cost, reasons).

The plan is made for the household's local calendar day (23, 24 or 25 hours on clock-change
days) because appliance windows in the household file are local clock hours. "Tomorrow" needs
tomorrow's prices to be published. The heat pump and solar are not part of this plan (no
verified weather forecast); the page says so.

## Security

- Content-Security-Policy: `default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'`.
  A test checks that the files contain no inline script, style or event handler, no outside URL,
  no `innerHTML` and no browser storage.
- All server text is placed with `textContent`.
- The key is typed into the page and kept in a JavaScript variable: not in storage, a cookie or the
  URL. Reloading the tab forgets it. The page itself is public; only the data routes check the key.
- The page is as safe as the connection: serve it over TLS (see `docs/deploy.md`).

## Try it with invented prices

```bash
python scripts/demo_prices.py data/demo_prices.csv      # synthetic prices, yesterday to tomorrow
export WATTMAGENT_API_KEYS=<a long random key>
export WATTMAGENT_PRICE_CSV=data/demo_prices.csv
export WATTMAGENT_HOUSEHOLD_FILE=config/households/example_battery.toml
export WATTMAGENT_UI_BANNER="SYNTHETIC DEMO DATA, not real prices"
uvicorn wattmagent.api.app:create_app --factory
# open http://127.0.0.1:8000/ui
```

## Checked and not checked

Checked: view-model and route tests, asset rules (CI), and a render in headless Chrome
(light, dark and a 390 px phone width) against the real planner with no console or policy errors.

Not checked: other browsers, a screen reader beyond the labels in the markup, a real host, real
prices or live data, many concurrent users.

## Measured consumption (optional)

Set three variables and the page reads measured energy from Home Assistant history:

```bash
export WATTMAGENT_HA_URL=http://homeassistant.local:8123
export WATTMAGENT_HA_TOKEN=<a long-lived access token>
export WATTMAGENT_HA_ENERGY_ENTITY=sensor.grid_import_energy   # cumulative, only counts up
export WATTMAGENT_HA_ENERGY_UNIT=kWh                           # or Wh
```

- **Read-only.** One `GET /api/history/period/...` for one `sensor.*` entity. It calls no service
  and writes nothing. A test checks the module never mentions services or states endpoints or POST.
- **No guessing.** An hour is shown only when the meter had a reading at or before both its start
  and its end and did not go backwards. A gap, `unavailable`, a reset or an hour not over yet is
  left empty, not estimated.
- **What the sensor measures decides what the dots mean.** A grid-import meter is comparable to
  the plan's grid bill; a whole-house meter is not (it counts solar self-use). The page does not
  know which you chose. Do not read a gap between dots and bars as an optimiser error.
- The token is read from the environment, never shown, and redirects are not followed. The answer
  is cached for a minute. If Home Assistant cannot be reached the page says so and keeps the plan.
- **Unverified:** the layout of the history reply is written from Home Assistant's documentation and
  tested against a fake server; it has not run against a real instance.
