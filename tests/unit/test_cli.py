from __future__ import annotations

import contextlib
import io
import sys

from wattmagent.backtest.__main__ import main


def test_cli_synthetic_output_is_labelled_and_lists_baselines() -> None:
    buf = io.StringIO()
    old = sys.argv
    sys.argv = ["wattmagent.backtest", "--synthetic", "--days", "10", "--seed", "1"]
    try:
        with contextlib.redirect_stdout(buf):
            main()
    finally:
        sys.argv = old
    out = buf.getvalue()
    assert "SYNTHETIC" in out  # synthetic runs must never look like real results
    assert "charge_immediately" in out and "hindsight bound" in out
