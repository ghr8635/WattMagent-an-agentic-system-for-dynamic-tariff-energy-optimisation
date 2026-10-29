"""Evaluation gate for the request parser: field-level accuracy must not regress."""

from __future__ import annotations

from wattmagent.graph.eval_cases import CASES
from wattmagent.graph.evaluation import evaluate
from wattmagent.graph.request import parse_with_rules

MIN_CASE_ACCURACY = 0.90  # share of cases with every field right
MIN_FIELD_ACCURACY = 0.95  # share of all fields right


def test_rule_parser_accuracy_gate() -> None:
    report = evaluate(parse_with_rules, CASES)
    assert (
        report.case_accuracy >= MIN_CASE_ACCURACY and report.field_accuracy >= MIN_FIELD_ACCURACY
    ), report.summary() + "\n" + "\n".join(report.misses)
