"""A small load test for a running WattMagent API.

    python scripts/load_test.py --url http://localhost:8000 --key $KEY \
        --requests 200 --concurrency 8

Reports throughput and latency percentiles per request kind and the status codes seen, then
exits non-zero if any answer was a 5xx or the p95 is above ``--p95-ms``. It measures the
server you point it at, on the machine you run it from: quote the numbers together with that
setup, never alone. The default rate limit (20 requests/minute per key) will answer 429 under
this load; raise WATTMAGENT_RATE_LIMIT_PER_MIN on the test server, or pass --accept-429.
"""

from __future__ import annotations

import argparse
import statistics
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import httpx

EV_TEXT = "charge my car to 80% by 7am, not before 22:00"
APPLIANCE_TEXT = "run the dishwasher by 10pm"


def one(client: httpx.Client, url: str, key: str, kind: str, text: str) -> tuple[str, int, float]:
    start = time.perf_counter()
    try:
        resp = client.post(
            f"{url}/v1/plans", json={"text": text}, headers={"X-API-Key": key}, timeout=30
        )
        code = resp.status_code
    except httpx.HTTPError:
        code = 0  # no answer at all
    return kind, code, (time.perf_counter() - start) * 1000


def percentile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(q * len(ordered)))]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--url", default="http://localhost:8000")
    ap.add_argument("--key", required=True)
    ap.add_argument("--requests", type=int, default=200)
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--p95-ms", type=float, default=1000.0)
    ap.add_argument("--appliances", action="store_true", help="mix in appliance requests")
    ap.add_argument("--accept-429", action="store_true", help="do not count rate limiting as bad")
    args = ap.parse_args()

    jobs = [
        ("ev", EV_TEXT) if (not args.appliances or i % 2 == 0) else ("appliance", APPLIANCE_TEXT)
        for i in range(args.requests)
    ]
    started = time.perf_counter()
    with httpx.Client() as client, ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        results = list(pool.map(lambda j: one(client, args.url, args.key, *j), jobs))
    wall = time.perf_counter() - started

    codes = Counter(code for _, code, _ in results)
    print(f"{len(results)} requests, concurrency {args.concurrency}, {wall:.1f} s wall")
    print(f"throughput {len(results) / wall:.1f} req/s; status codes {dict(sorted(codes.items()))}")
    ok = True
    for kind in sorted({k for k, _, _ in results}):
        ms = [t for k, c, t in results if k == kind and c == 200]
        if not ms:
            print(f"{kind}: no successful answers")
            ok = False
            continue
        p95 = percentile(ms, 0.95)
        print(
            f"{kind}: n={len(ms)} p50={statistics.median(ms):.0f} ms "
            f"p95={p95:.0f} ms max={max(ms):.0f} ms"
        )
        ok = ok and p95 <= args.p95_ms
    bad = sum(n for c, n in codes.items() if c == 0 or c >= 500)
    if not args.accept_429:
        bad += codes.get(429, 0)
    if bad:
        print(f"FAIL: {bad} answers were 5xx, missing or rate limited")
        ok = False
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
