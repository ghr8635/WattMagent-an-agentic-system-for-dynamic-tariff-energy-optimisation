# Home Assistant: advice mode

WattMagent can hand a verified appliance plan to Home Assistant as **advice**. A person sees
"start the dishwasher at 03:00, about 0.08 EUR cheaper" and decides. **Nothing is switched
automatically**: this integration calls no Home Assistant service, and a test checks that.

Status: built and tested against a small fake server over real HTTP
(`tests/unit/test_homeassistant.py`). **Not yet run against a real Home Assistant**: how a real
instance treats these calls is unverified.

## What it publishes

Two calls per plan, nothing else (the client refuses any other path, entity or event):

| Call | Purpose |
|---|---|
| `POST /api/states/sensor.wattmagent_advice` | the advice as a sensor: state `ready`, `fallback` or `no_plan`, the plan in the attributes |
| `POST /api/events/wattmagent_advice` | the same content as an event, for automations that react to a new plan |

Attributes: `items` (one per appliance cycle with `start_at`, `end_at`, `energy_kwh`, `cost_eur`,
`saving_eur`, `expires_at`; and hour lists for the home battery and hot-water tank),
`valid_until`, `decision`, `message`, `request_id` (the same id as in the audit log),
`price_basis`, `generated_at`, `mode: advice`, `actuation: false`.

- `ready`: the optimised plan, verified.
- `fallback`: the safe default (every appliance starts as early as its window allows) because
  prices were stale or the plan failed verification. Do not present it as a saving.
- `no_plan`: the request could not be planned (a question or an error); no items.
- **Every item expires.** Advice whose `expires_at` has passed must not be acted on.
  `valid_until` is the latest of them.

## Safety rules (each is tested)

- **Dry run by default.** Nothing is sent without `--publish`.
- Only `sensor.wattmagent_*` entities and `wattmagent_*` events can be written, so it cannot
  overwrite another device's state or fire another event.
- No redirect is followed (it could carry the token elsewhere), requests time out after 10 s,
  and the connection ignores proxy settings (Home Assistant is normally on the local network).
- The token comes from the environment only, is never in an argument, a log line, an error message
  or what is sent in the body, and is hidden in `repr`.
- A failed publish exits non-zero and says what failed, without the token.

## Use

In Home Assistant: your profile page, *Security*, *Long-lived access tokens*, create one. Then,
with the WattMagent API running and one of its keys:

```powershell
$env:WATTMAGENT_ADVICE_API_KEY = "<one of your API keys>"
$env:WATTMAGENT_HA_URL = "http://homeassistant.local:8123"
$env:WATTMAGENT_HA_TOKEN = "<the long-lived token>"

# 1. dry run: shows the plan and the two calls it would make, sends nothing
python -m wattmagent.integrations.homeassistant --text "run the dishwasher before 8pm"

# 2. really publish
python -m wattmagent.integrations.homeassistant --text "run the dishwasher before 8pm" --publish
```

`--from-file answer.json` uses a saved answer of `POST /v1/plans` instead of asking the API.
The household must be configured on the API (`WATTMAGENT_HOUSEHOLD_FILE`, see
`docs/appliances.md`). Plain `http://` is normal on a home network; use `https://` if Home
Assistant is reachable from elsewhere.

## Example automation (not tested against a real Home Assistant)

```yaml
automation:
  - alias: WattMagent advice
    trigger:
      - platform: event
        event_type: wattmagent_advice
    condition:
      - condition: template
        value_template: "{{ trigger.event.data.state == 'ready' }}"
    action:
      - service: notify.mobile_app_YOUR_PHONE   # your own notify service
        data:
          title: "WattMagent"
          message: "{{ trigger.event.data.message }}"
```

## Limits and open points

- **Not run against a real Home Assistant.** As far as I know, states created through the REST
  API are not restored after a Home Assistant restart; republish after a restart (unverified).
- No schedule: run the command yourself, or from a task scheduler (Windows Task Scheduler, cron).
  A scheduled publisher inside the service is not built.
- The advice covers appliance cycles, the home battery and the hot-water tank. The EV plan, the
  heat pump and solar are not published (the heat pump and solar need a verified forecast).
- Plain `http://` sends the token unencrypted on your network.
- Automatic control (switching devices on the plan) is deliberately not built. It would need an
  opt-in, per-device limits, a kill switch and weeks of advice mode to compare plans with what
  people actually did.

## Reading measured consumption (history, read-only)

Separate from advice mode: the API can read one cumulative energy sensor from Home Assistant
history to show measured use on the web page. It only issues `GET /api/history/period/...`; see
docs/web-ui.md. Unverified against a real instance.
