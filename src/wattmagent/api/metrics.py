"""Prometheus-style metrics without a dependency.

Small on purpose: counters, one histogram and callback gauges, rendered in the text
exposition format (version 0.0.4). Labels must come from fixed sets (route templates,
status codes, plan outcomes), never from request content; as a second guard each metric
keeps at most ``MAX_SERIES`` label combinations and ignores new ones beyond that.
Counters live in memory, so they reset on restart and are per process, which is how
Prometheus expects counters to behave.
"""

from __future__ import annotations

import math
import re
import threading
from collections.abc import Callable, Sequence

MAX_SERIES = 1000
_NAME = re.compile(r"^[a-zA-Z_:][a-zA-Z0-9_:]*$")
DURATION_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)
_KNOWN_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"})


def _number(value: float) -> str:
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "+Inf" if value > 0 else "-Inf"
    return str(int(value)) if value == int(value) and abs(value) < 1e15 else repr(value)


def _escape(text: str, quotes: bool = False) -> str:
    text = text.replace("\\", "\\\\").replace("\n", "\\n")
    return text.replace('"', '\\"') if quotes else text


def _label_block(
    names: Sequence[str], values: Sequence[str], extra: Sequence[tuple[str, str]] = ()
) -> str:
    pairs = [*zip(names, values, strict=True), *extra]
    if not pairs:
        return ""
    return "{" + ",".join(f'{k}="{_escape(v, quotes=True)}"' for k, v in pairs) + "}"


def _check_name(name: str, labels: Sequence[str]) -> None:
    if not _NAME.match(name) or not all(_NAME.match(label) for label in labels):
        raise ValueError(f"invalid metric or label name: {name!r} {tuple(labels)!r}")


class Counter:
    def __init__(self, name: str, help_text: str, labels: Sequence[str] = ()) -> None:
        _check_name(name, labels)
        self.name, self.help, self.labels = name, help_text, tuple(labels)
        self._values: dict[tuple[str, ...], float] = {}
        self._lock = threading.Lock()

    def inc(self, *label_values: str, amount: float = 1.0) -> None:
        if len(label_values) != len(self.labels) or amount < 0:
            raise ValueError(f"{self.name}: wrong label count or negative amount")
        with self._lock:
            if label_values not in self._values and len(self._values) >= MAX_SERIES:
                return
            self._values[label_values] = self._values.get(label_values, 0.0) + amount

    def value(self, *label_values: str) -> float:
        with self._lock:
            return self._values.get(label_values, 0.0)

    def render(self) -> list[str]:
        lines = [f"# HELP {self.name} {_escape(self.help)}", f"# TYPE {self.name} counter"]
        with self._lock:
            items = sorted(self._values.items())
        if not items and not self.labels:
            items = [((), 0.0)]  # an unlabelled counter is exported at 0 from the start
        lines += [f"{self.name}{_label_block(self.labels, k)} {_number(v)}" for k, v in items]
        return lines


class Histogram:
    def __init__(
        self,
        name: str,
        help_text: str,
        labels: Sequence[str] = (),
        buckets: Sequence[float] = DURATION_BUCKETS,
    ) -> None:
        _check_name(name, labels)
        if "le" in labels:
            raise ValueError("'le' is reserved for histogram buckets")
        self.name, self.help, self.labels = name, help_text, tuple(labels)
        self.buckets = tuple(sorted(buckets))
        # per label set: [count per bucket..., sum, count]
        self._state: dict[tuple[str, ...], list[float]] = {}
        self._lock = threading.Lock()

    def observe(self, value: float, *label_values: str) -> None:
        if len(label_values) != len(self.labels):
            raise ValueError(f"{self.name}: wrong label count")
        with self._lock:
            state = self._state.get(label_values)
            if state is None:
                if len(self._state) >= MAX_SERIES:
                    return
                state = self._state[label_values] = [0.0] * (len(self.buckets) + 2)
            for i, bound in enumerate(self.buckets):
                if value <= bound:
                    state[i] += 1
            state[-2] += value
            state[-1] += 1

    def render(self) -> list[str]:
        lines = [f"# HELP {self.name} {_escape(self.help)}", f"# TYPE {self.name} histogram"]
        with self._lock:
            items = sorted((k, list(v)) for k, v in self._state.items())
        for key, state in items:
            for bound, count in zip(self.buckets, state, strict=False):
                block = _label_block(self.labels, key, [("le", _number(bound))])
                lines.append(f"{self.name}_bucket{block} {_number(count)}")
            inf = _label_block(self.labels, key, [("le", "+Inf")])
            lines.append(f"{self.name}_bucket{inf} {_number(state[-1])}")
            lines.append(f"{self.name}_sum{_label_block(self.labels, key)} {_number(state[-2])}")
            lines.append(f"{self.name}_count{_label_block(self.labels, key)} {_number(state[-1])}")
        return lines


class GaugeFunc:
    """A gauge read at scrape time. ``fn`` returning None means "no value yet": no sample."""

    def __init__(
        self,
        name: str,
        help_text: str,
        fn: Callable[[], float | None],
        labels: dict[str, str] | None = None,
    ) -> None:
        _check_name(name, list(labels or {}))
        self.name, self.help, self._fn = name, help_text, fn
        self._labels = labels or {}

    def render(self) -> list[str]:
        lines = [f"# HELP {self.name} {_escape(self.help)}", f"# TYPE {self.name} gauge"]
        value = self._fn()
        if value is not None:
            block = _label_block(list(self._labels), list(self._labels.values()))
            lines.append(f"{self.name}{block} {_number(value)}")
        return lines


Metric = Counter | Histogram | GaugeFunc


class Registry:
    def __init__(self) -> None:
        self._metrics: list[Metric] = []

    def register[M: (Counter, Histogram, GaugeFunc)](self, metric: M) -> M:
        if any(m.name == metric.name for m in self._metrics):
            raise ValueError(f"metric already registered: {metric.name}")
        self._metrics.append(metric)
        return metric

    def render(self) -> str:
        return "\n".join(line for m in self._metrics for line in m.render()) + "\n"


class ApiMetrics:
    """Every metric the API exports. Names and meanings are documented in docs/observability.md."""

    def __init__(self, version: str) -> None:
        self.registry = Registry()
        reg = self.registry.register
        self.requests = reg(
            Counter(
                "wattmagent_http_requests_total",
                "HTTP requests by method, route template and status code.",
                ("method", "route", "status"),
            )
        )
        self.duration = reg(
            Histogram(
                "wattmagent_http_request_duration_seconds",
                "HTTP request duration in seconds by route template.",
                ("route",),
            )
        )
        self.plans = reg(
            Counter(
                "wattmagent_plan_results_total",
                "Plan requests by outcome (planned, needs_input, error) and decision "
                "(optimised, fallback, none).",
                ("status", "decision"),
            )
        )
        self.replays = reg(
            Counter(
                "wattmagent_idempotent_replays_total",
                "Requests answered from the idempotency store.",
            )
        )
        self.replans = reg(
            Counter(
                "wattmagent_replans_total",
                "Replan requests by outcome: planned, needs_attention, not_found, rejected.",
                ("outcome",),
            )
        )
        self.rate_limited = reg(
            Counter("wattmagent_rate_limited_total", "Requests refused with 429 by the rate limit.")
        )
        self.auth_failures = reg(
            Counter(
                "wattmagent_auth_failures_total",
                "Requests refused with 401 (missing or wrong key).",
            )
        )
        self.price_age_fn: Callable[[], float | None] = lambda: None
        reg(
            GaugeFunc(
                "wattmagent_price_age_hours",
                "Age of the prices at last use, from their fetch time. Absent before first use.",
                lambda: self.price_age_fn(),
            )
        )
        reg(
            GaugeFunc(
                "wattmagent_build_info",
                "Build information; the value is always 1.",
                lambda: 1.0,
                labels={"version": version},
            )
        )

    def observe_request(self, method: str, route: str, status: int, seconds: float) -> None:
        verb = method if method in _KNOWN_METHODS else "OTHER"
        self.requests.inc(verb, route, str(status))
        self.duration.observe(seconds, route)

    def render(self) -> str:
        return self.registry.render()
