# API

Run it (keys are your own random strings of 16+ characters). Live prices from SMARD need no file:

```powershell
$env:WATTMAGENT_API_KEYS = "<key1>,<key2>"
$env:WATTMAGENT_PRICE_SOURCE = "smard"
python -m uvicorn --factory wattmagent.api.app:create_app --port 8000
```

Or plan on a price file you converted yourself:

```powershell
$env:WATTMAGENT_API_KEYS = "<key1>,<key2>"
$env:WATTMAGENT_PRICE_CSV = "data\prices_hourly_utc.csv"
python -m uvicorn --factory wattmagent.api.app:create_app --port 8000
```

The app refuses to start without keys, or with `WATTMAGENT_PRICE_SOURCE=csv` (the default)
and no price file. The language backend is
`rules` unless `WATTMAGENT_LANGUAGE_BACKEND=llm`.

## Endpoints

| Method and path | Auth | Purpose |
|---|---|---|
| `GET /healthz` | none | the process is up |
| `GET /readyz` | none | 200 if prices are present, fresh and cover the next 24 h, else 503 with reasons |
| `POST /v1/plans` | `X-API-Key` | body `{"text": "90% by 6:30, not before 22:00"}` (1-500 characters) |

`POST /v1/plans` answers 200 with `status` `planned` or `needs_input` (the questions are in
`message`), 422 for an unusable request (`status: error`, or a malformed body), 401 for a
missing or wrong key, 429 with `Retry-After` when the limit is exceeded, 409 for an
idempotency conflict, 503 when prices cannot be read. The body of a plan holds the
explanation, the decision (`optimised` or `fallback`), whether the request was satisfied,
the facts behind the explanation and the request id. The agent's internal trace is not
returned.

## Behaviour worth knowing

- **Order of checks:** key, rate limit, body, idempotency, agent.
- **Request ID:** `X-Request-ID` on every response. A client-sent id is kept if it is up
  to 64 characters of letters, digits, `.`, `_`, `-`; otherwise a new one is generated. The id
  is also written to the audit record.
- **Idempotency:** send `Idempotency-Key` (8-128 characters). A repeat with the same key and
  text returns the stored answer with `Idempotent-Replay: true` and does not plan again. The
  same key with different text is 409. Entries live 24 h. A replay keeps the original
  body, including its original `request_id`.
- **Logs** contain request id, method, path, status, duration and a short key id; never keys,
  request text or prices.
- **Live prices** (`smard`): fetched at most every `WATTMAGENT_PRICE_REFRESH_S` seconds
  (default 900). The age that counts is when *we* fetched. If SMARD cannot be reached, the
  previous prices keep being served with their old fetch time, so after 36 h the freshness
  check forces the safe fallback. A missing hour is caught the same way: `/readyz` stays
  "not ready" until the feed covers the next 24 h. A refresh waits on a lock, so requests
  arriving during a refresh wait for it (at most the 10 s fetch timeout per file).
- **Limits of this version:** rate limit and idempotency are in memory, so they hold per
  process; a second replica would count separately. Base load is synthetic. With the `csv` source,
  prices come from a file whose modification time stands in for the fetch time, which
  re-converting an old export can fake; the `smard` source records the real fetch time.
