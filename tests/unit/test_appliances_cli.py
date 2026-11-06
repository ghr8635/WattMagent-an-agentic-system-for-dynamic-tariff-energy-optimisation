from __future__ import annotations

import contextlib
import io
import json
import sys
from pathlib import Path

import pytest

from wattmagent.appliances.__main__ import main

EXAMPLE = Path(__file__).resolve().parents[2] / "config" / "households" / "example.toml"


def run(*args: str) -> str:
    buf = io.StringIO()
    old = sys.argv
    sys.argv = ["wattmagent.appliances", *args]
    try:
        with contextlib.redirect_stdout(buf):
            main()
    finally:
        sys.argv = old
    return buf.getvalue()


def test_cli_synthetic_run_is_labelled_and_writes_an_audit_log(tmp_path: Path) -> None:
    log = tmp_path / "audit.jsonl"
    out = run("--household", str(EXAMPLE), "--synthetic", "--days", "6", "--audit-log", str(log))
    assert "SYNTHETIC prices" in out and "ILLUSTRATIVE ASSUMPTIONS" in out
    assert "milp_optimiser" in out and "0 fallbacks" in out
    lines = log.read_text(encoding="utf-8").splitlines()
    assert lines and json.loads(lines[0])["kind"] == "appliances"


def test_cli_with_csv_and_tariff(tmp_path: Path) -> None:
    rows = ["timestamp,price_eur_mwh"]
    for d in range(1, 5):
        for h in range(24):
            rows.append(f"2025-02-{d:02d}T{h:02d}:00:00+00:00,{60 + 10 * (h % 7)}")
    csv = tmp_path / "p.csv"
    csv.write_text("\n".join(rows), encoding="utf-8")
    tariff = (
        Path(__file__).resolve().parents[2]
        / "config"
        / "tariffs"
        / "illustrative_de_household.toml"
    )
    out = run("--household", str(EXAMPLE), "--csv", str(csv), "--tariff", str(tariff))
    assert "price file: p.csv" in out and "ILLUSTRATIVE TARIFF" in out


def test_cli_rejects_a_missing_household(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        run("--household", str(tmp_path / "nope.toml"), "--synthetic")
