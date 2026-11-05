# Deployment

## What exists

| Piece | File | State |
|---|---|---|
| Image | `deploy/Dockerfile` | multi-stage, non-root (uid 10001), healthcheck on `/healthz` |
| Build context filter | `.dockerignore` | keeps `.env`, `.git`, caches, data and tests out of the build |
| Compose | `deploy/compose.yaml` | API behind Caddy; read-only root filesystem, all capabilities dropped, `no-new-privileges`, named volumes for the audit log and caches |
| Settings | `.env.example` | copy to `.env` on the host; never commit `.env` |
| Smoke test | `scripts/smoke_test.sh` | runs in CI on every PR against the freshly built image |
| Deploy and rollback | `deploy/deploy.sh` | written, **untested**: there is no host yet |

`/metrics` is blocked at the Caddy proxy (404 from outside) and meant to be scraped from
inside the network, for example by a Prometheus container on the same compose network
that targets `api:8000`.

## What the smoke test proves

It starts the image the way production runs it (`--read-only`, `--cap-drop ALL`,
`no-new-privileges`) on a small generated price file, then checks: `/healthz` answers,
`/readyz` is ready, the process is not root, a request without a key or with a wrong key
is a 401, an empty request is a 422, a plan request returns 200 with `X-Request-ID`,
and repeating it with the same `Idempotency-Key` is a byte-identical replay. It uses no
internet and no real prices, so it cannot fail because SMARD is down. It does **not**
test live SMARD fetching, the language-model backend, load, or TLS.

## Run it on a host

```bash
cp .env.example .env        # then set WATTMAGENT_API_KEYS to long random keys
cd deploy
TAG=latest docker compose up -d
curl -fsS http://localhost/healthz
```

Create keys with, for example, `python -c "import secrets; print(secrets.token_urlsafe(32))"`.
Rotate a key by adding the new one next to the old (comma-separated), restarting, moving
clients over, then removing the old one.

## Release, roll forward, roll back

1. Tag `v0.2.0` on `main`: the Release workflow builds and pushes
   `ghcr.io/<owner>/<repo>:0.2.0` (and a `sha-...` tag).
2. On the host: `TAG=0.2.0 ./deploy.sh`. It pulls, starts the new tag, waits for
   `/healthz`, records the tag as good, and if the health check fails restarts the
   previous good tag. Only liveness decides the rollback: `/readyz` can fail because the
   price feed is down, and an older image would not fix that.
3. Manual rollback: `TAG=<previous> docker compose up -d api`.

## Known gaps (also in docs/production-checklist.md)

- Base image and dependencies are not pinned by digest or lockfile yet.
- Rate limit and idempotency live in memory, so run **one** replica until they move to a
  shared store.
- No TLS configuration beyond Caddy's defaults for a real domain (`SITE_ADDRESS`).
- `deploy.sh` has never run against a real host.
