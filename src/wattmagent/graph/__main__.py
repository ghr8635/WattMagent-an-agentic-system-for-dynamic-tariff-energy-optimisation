"""CLI: ``python -m wattmagent.graph "80% by 7am" --csv data/prices_hourly_utc.csv``.

Without ``--llm`` only the rule parser and the template explanation are used (no network).
``--llm`` uses the configured Hugging Face models (set WATTMAGENT_HF_TOKEN).
``--llm-check`` only tests which configured models answer, then exits.
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from wattmagent.data.prices import load_csv
from wattmagent.graph import AgentContext, AgentDeps, run_agent
from wattmagent.safety.audit import InMemoryAuditLog


def main() -> None:
    p = argparse.ArgumentParser(description="Plan EV charging from a plain-language request")
    p.add_argument("text", nargs="?", help='for example: "90% by 6:30, not before 22:00"')
    p.add_argument("--csv", type=Path, help="price CSV (timestamp,price_eur_mwh), UTC")
    p.add_argument("--now", help="ISO time with offset, e.g. 2026-03-10T17:20+00:00")
    p.add_argument("--tz", default="Europe/Berlin")
    p.add_argument("--llm", action="store_true", help="use the Hugging Face models")
    p.add_argument("--llm-check", action="store_true", help="test the configured models, then exit")
    p.add_argument(
        "--llm-eval",
        action="store_true",
        help="grade the model-backed parser on the labelled cases",
    )
    p.add_argument("--engine", choices=["simple", "langgraph"], default="simple")
    p.add_argument("--json", action="store_true", help="print the full response as JSON")
    args = p.parse_args()

    if args.llm_check:
        from wattmagent.core.config import Settings
        from wattmagent.llm.factory import check_models

        settings = Settings()
        for model, ok, detail in check_models(settings):
            print(f"{'OK  ' if ok else 'FAIL'} {model}: {detail}")
        return
    if args.llm_eval:
        from wattmagent.core.config import Settings
        from wattmagent.graph.eval_cases import CASES
        from wattmagent.graph.evaluation import evaluate
        from wattmagent.graph.request import parse_request
        from wattmagent.llm.factory import build_client

        client = build_client(Settings())
        report = evaluate(lambda t: parse_request(t, client)[0], CASES)
        print(report.summary())
        print("\n".join(f"MISS {m}" for m in report.misses) or "no misses")
        return
    if not args.text or not args.csv:
        p.error("text and --csv are required")

    tz = ZoneInfo(args.tz)
    prices = load_csv(args.csv)
    now = (
        datetime.fromisoformat(args.now)
        if args.now
        else (prices[-1].start - timedelta(hours=30)).replace(minute=20)
    )
    llm = None
    if args.llm:
        from wattmagent.core.config import Settings
        from wattmagent.llm.factory import build_client

        llm = build_client(Settings())
    ctx = AgentContext(prices=prices, fetched_at=now, tz=tz)
    audit = InMemoryAuditLog()
    resp = run_agent(
        args.text, AgentDeps(ctx, llm=llm, audit=audit), now.astimezone(UTC), engine=args.engine
    )
    if args.json:
        print(
            json.dumps(
                {
                    "status": resp.status,
                    "message": resp.message,
                    "decision": resp.decision,
                    "facts": resp.facts,
                    "trace": resp.trace,
                },
                indent=2,
                default=str,
            )
        )
        return
    print(f"# demo: price file treated as freshly fetched at {now.isoformat()}")
    print(f"# prices: {ctx.price_basis}; base load is SYNTHETIC")
    print(f"status: {resp.status}\n{resp.message}\n")
    print("trace: " + " | ".join(resp.trace))
    print(f"model calls: {resp.usage.llm_calls}, tokens: {resp.usage.total_tokens}")


if __name__ == "__main__":
    main()
