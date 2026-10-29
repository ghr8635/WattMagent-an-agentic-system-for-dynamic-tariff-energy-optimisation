"""Grading a request parser against labelled cases."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from wattmagent.graph.eval_cases import Case
from wattmagent.graph.request import ParsedRequest


@dataclass(frozen=True, slots=True)
class EvalReport:
    cases: int
    cases_ok: int
    fields_ok: int
    fields_total: int
    from_model: int  # answers that came from the model (the rest fell back to the rules)
    misses: list[str] = field(default_factory=list)

    @property
    def case_accuracy(self) -> float:
        return self.cases_ok / self.cases

    @property
    def field_accuracy(self) -> float:
        return self.fields_ok / self.fields_total

    def summary(self) -> str:
        return (
            f"cases fully right: {self.cases_ok}/{self.cases} ({self.case_accuracy:.0%}); "
            f"fields right: {self.fields_ok}/{self.fields_total} ({self.field_accuracy:.0%}); "
            f"answered by the model: {self.from_model}/{self.cases}"
        )


def _clock(t: object) -> str | None:
    return None if t is None else f"{t:%H:%M}"


def evaluate(parse: Callable[[str], ParsedRequest], cases: Sequence[Case]) -> EvalReport:
    misses: list[str] = []
    fields_ok = fields_total = cases_ok = from_model = 0
    for text, target, depart, earliest, current in cases:
        p = parse(text)
        from_model += p.source == "llm"
        got = (p.target_percent, _clock(p.depart), _clock(p.earliest_start), p.current_percent)
        want = (target, depart, earliest, current)
        hits = [g == w for g, w in zip(got, want, strict=True)]
        fields_ok += sum(hits)
        fields_total += len(hits)
        cases_ok += all(hits)
        if not all(hits):
            misses.append(f"{text!r}: got {got}, wanted {want}")
    return EvalReport(len(cases), cases_ok, fields_ok, fields_total, from_model, misses)
