# Agent

Turns a request in plain language into a verified charging plan and an explanation.

```
"90% by 6:30, not before 22:00"
        |
 parse_request   model first (JSON, grounded in the text), rules as fallback
        |
 validate_request  bounds, real instants, conservative rounding; asks if unclear  --> END
        |
 plan            plan_with_safety: fresh prices? optimiser? verifier? else fallback
        |
 explain         template from the verified numbers; the model may rephrase it,
        |        but only if it adds no number that is not in the plan            --> END
```

## Who decides what
| Question | Decided by |
|---|---|
| Which hours to charge, how fast | the optimiser (never the model) |
| Whether a plan is safe | the verifier (never the model) |
| What the user meant | the model, checked by rules and the validator |
| How the plan is worded | the model, checked so it cannot add figures |

## What protects against a misbehaving model
- **Grounding:** every number in the model's JSON (percentages, hours, minutes) must also
  occur in the user's text, otherwise the reply is discarded and the rule parser is used.
  This stops invented values and values smuggled in by an injected instruction.
- **Validation:** target 1-100%, current 0-100%, a departure at least one full hour away and
  within 24 h, earliest start before departure. Anything else becomes a question.
- **Conservative rounding:** departure is rounded *down* to the full hour, earliest start *up*;
  the explanation says so ("Departure 06:30 is planned as 06:00").
- **Assumptions are stated:** a missing target (80%) or current charge (30%) is assumed and
  said out loud, never silently.
- **Explanation check:** every number in the model's wording must exist in the template or
  the plan facts; otherwise the template is used.
- **Failure modes:** model down, over budget, malformed reply -> rule parser and template; the
  agent still plans. Missing or old prices -> safe fallback plan and no cost figures.
- Input over 500 characters or empty is refused before any model call.

## Engines
`run_agent(..., engine="simple")` runs the flow with a small built-in executor.
`engine="langgraph"` runs the identical nodes and routes as a LangGraph `StateGraph`
(`graph/langgraph_app.py`). A test requires both to give the same answer. The LangGraph
adapter is excluded from strict type checking (see `pyproject.toml`).

## Try it (no network, rules only)
```
python -m wattmagent.graph "90% by 6:30, not before 22:00" --csv data/prices_hourly_utc.csv
```
The price file is treated as freshly fetched; the base load is synthetic.

## Check the real model on your token
Set `WATTMAGENT_HF_TOKEN` (PowerShell: `$env:WATTMAGENT_HF_TOKEN = "hf_..."`; never commit it), then:
```
python -m wattmagent.graph --llm-check
python -m wattmagent.graph "90% by 6:30, not before 22:00" --csv data/prices_hourly_utc.csv --llm
```
The first line per model says OK or FAIL with the reason. Model names come from
`WATTMAGENT_LLM_MODELS` (comma separated).

## Hugging Face access (what we found on 2026-10-29)
`--llm-check` on a free Hugging Face account returned `HTTP 400` for `Qwen/Qwen2.5-7B-Instruct`
and `402 credits exhausted` for `meta-llama/Llama-3.1-8B-Instruct`. Hugging Face's pricing page
([Inference Providers pricing](https://huggingface.co/docs/inference-providers/pricing)) states that
free accounts get no monthly credits for routed Inference Providers; PRO and paid credits are
needed. The 400 means the router rejected the request for that model (the status code alone does
not say why; the error now includes a short excerpt of the provider's message).

What this means: the agent is built to work without the model (rules and template), which
the run on 2026-10-29 confirmed, but the **real-model path is unverified**. Options:
1. Buy a small amount of credits, or upgrade, then re-run `--llm-check`.
2. Run an open model from the Hub on your own computer with Ollama (free, no token): see
   `docs/local-models.md`. The client only needs a different base URL and model name.
3. Point the client at another OpenAI-compatible endpoint (`WATTMAGENT_HF_BASE_URL`,
   `WATTMAGENT_HF_TOKEN`, `WATTMAGENT_LLM_MODELS`). Check that provider's current terms.
4. Keep running rules-only and say so in the README.

If the model is out of credits or the token is rejected, it is called at most once per request.

## Evaluation
`src/wattmagent/graph/eval_cases.py` holds 30 labelled requests; CI fails if the rule parser drops below
90% of cases fully right or 95% of fields. **Caveat:** the cases were written together with the
parser, so this is a regression guard, not a measure of accuracy on real users' wording. The
model parser has no real-model evaluation yet (tests use a scripted fake model).

## Not done yet
Replanning when conditions change, a conversation memory, execution on devices, and the API
around `run_agent`.
