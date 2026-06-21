# test_bench_summary.py — Tests for bench/_summary.py (the CI step-summary
# renderer).  Lives under tests/ so the regular unit-test job locks its
# contract, mirroring test_bench_compare.py for _compare (S42).

from __future__ import annotations

import json
from pathlib import Path

from bench._summary import render


def _write(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data))


def test_renders_rows(tmp_path: Path) -> None:
    p = tmp_path / "b.json"
    _write(
        p,
        {"benchmarks": [{"name": "test_a", "stats": {"median": 0.0001, "min": 9e-5}}]},
    )
    out = render(str(p))
    assert "test_a" in out
    assert "10,000" in out  # ops/s = 1 / 0.0001


def test_empty_benchmarks_header_only(tmp_path: Path) -> None:
    p = tmp_path / "b.json"
    _write(p, {"benchmarks": []})
    out = render(str(p))
    assert "| benchmark |" in out
    # Header + separator only, no data rows.
    assert len(out.splitlines()) == 2


def test_missing_benchmarks_key_no_keyerror(tmp_path: Path) -> None:
    """A malformed JSON without the 'benchmarks' key must not raise KeyError —
    it should degrade to a header-only table."""
    p = tmp_path / "b.json"
    _write(p, {})
    out = render(str(p))
    assert "| benchmark |" in out


def test_zero_median_no_zerodivision(tmp_path: Path) -> None:
    """A zero median (impossible for a real run, possible for malformed input)
    must not raise ZeroDivisionError on the ops/s column."""
    p = tmp_path / "b.json"
    _write(
        p, {"benchmarks": [{"name": "test_z", "stats": {"median": 0.0, "min": 0.0}}]}
    )
    out = render(str(p))
    assert "test_z" in out
    assert "—" in out
