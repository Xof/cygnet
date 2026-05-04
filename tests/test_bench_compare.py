# test_bench_compare.py — Tests for bench/_compare.py (the PR
# regression-comparison script).  Lives under tests/ rather than
# bench/ because tests/ is the only path collected by `just test`,
# and we want this script's contract locked in by the regular CI
# unit-test job — not just the advisory bench job.

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bench._compare import NOISE_THRESHOLD_PCT, render


def _write_json(path: Path, benchmarks: list[dict]) -> None:
    """Write a minimal pytest-benchmark-shaped JSON for the script to read."""
    path.write_text(json.dumps({"benchmarks": benchmarks}))


def _benchmark(fullname: str, median_seconds: float) -> dict:
    return {"fullname": fullname, "stats": {"median": median_seconds}}


class TestRender:
    def test_within_noise_band_no_bold(self, tmp_path: Path) -> None:
        """A small slowdown stays plain text — runner variance shouldn't
        be loud."""
        baseline = tmp_path / "b.json"
        current = tmp_path / "c.json"
        # 5% slower: well under NOISE_THRESHOLD_PCT
        _write_json(baseline, [_benchmark("a::T::test", 0.000100)])
        _write_json(current, [_benchmark("a::T::test", 0.000105)])

        out = render(str(baseline), str(current))
        assert "↑ +5.0%" in out
        # No bold wrapping the delta cell.
        assert "**↑" not in out

    def test_over_threshold_bolded(self, tmp_path: Path) -> None:
        """Real regression (>15%) gets bold so it can't be missed."""
        baseline = tmp_path / "b.json"
        current = tmp_path / "c.json"
        _write_json(baseline, [_benchmark("a::T::test", 0.000100)])
        _write_json(current, [_benchmark("a::T::test", 0.000125)])  # +25%
        out = render(str(baseline), str(current))
        assert "**↑ +25.0%**" in out

    def test_speedup_arrow_but_not_bold(self, tmp_path: Path) -> None:
        """Improvements get a down-arrow, no bold — surfacing slowdowns
        is the job, not celebrating wins."""
        baseline = tmp_path / "b.json"
        current = tmp_path / "c.json"
        _write_json(baseline, [_benchmark("a::T::test", 0.000100)])
        _write_json(current, [_benchmark("a::T::test", 0.000050)])  # -50%
        out = render(str(baseline), str(current))
        assert "↓ -50.0%" in out
        assert "**↓" not in out

    def test_new_benchmark(self, tmp_path: Path) -> None:
        baseline = tmp_path / "b.json"
        current = tmp_path / "c.json"
        _write_json(baseline, [])
        _write_json(current, [_benchmark("a::T::test_new", 0.000050)])
        out = render(str(baseline), str(current))
        assert "**new**" in out
        assert "test_new" in out

    def test_removed_benchmark(self, tmp_path: Path) -> None:
        baseline = tmp_path / "b.json"
        current = tmp_path / "c.json"
        _write_json(baseline, [_benchmark("a::T::test_gone", 0.000050)])
        _write_json(current, [])
        out = render(str(baseline), str(current))
        assert "*removed*" in out
        assert "test_gone" in out

    def test_short_name_uses_class_method(self, tmp_path: Path) -> None:
        """File path is stripped from the rendered name; class::method
        is what readers actually scan for."""
        baseline = tmp_path / "b.json"
        current = tmp_path / "c.json"
        full = "bench/test_render.py::TestSelect::test_simple"
        _write_json(baseline, [_benchmark(full, 0.000010)])
        _write_json(current, [_benchmark(full, 0.000010)])
        out = render(str(baseline), str(current))
        assert "TestSelect::test_simple" in out
        assert "bench/test_render.py" not in out

    def test_threshold_is_strict_greater_than(self, tmp_path: Path) -> None:
        """Exactly NOISE_THRESHOLD_PCT shouldn't bold — only > does.
        Locks the threshold semantics so future tuning is intentional."""
        baseline = tmp_path / "b.json"
        current = tmp_path / "c.json"
        # Construct a delta exactly at the threshold.
        b_med = 0.000100
        c_med = b_med * (1 + NOISE_THRESHOLD_PCT / 100)
        _write_json(baseline, [_benchmark("a::T::test", b_med)])
        _write_json(current, [_benchmark("a::T::test", c_med)])
        out = render(str(baseline), str(current))
        assert "**↑" not in out  # exactly threshold = not bolded


def test_invalid_args_exits_2(tmp_path: Path) -> None:
    """Argparse-style usage error — the CLI guard returns 2, not 1."""
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-m", "bench._compare"],
        capture_output=True,
    )
    assert result.returncode == 2
    assert b"usage:" in result.stderr.lower()


def test_compile_smoke() -> None:
    """The script must compile — catches indentation / typo bugs that
    might otherwise show up only when CI tries to run it."""
    import bench._compare  # noqa: F401  (imported for side effect)


def test_invocable_via_module_dash_m(tmp_path: Path) -> None:
    """End-to-end CLI invocation: write two JSONs, exercise the same
    `python -m bench._compare baseline.json current.json` form CI uses."""
    import subprocess
    import sys

    baseline = tmp_path / "b.json"
    current = tmp_path / "c.json"
    _write_json(baseline, [_benchmark("a::T::test", 0.0001)])
    _write_json(current, [_benchmark("a::T::test", 0.0001)])

    result = subprocess.run(
        [sys.executable, "-m", "bench._compare", str(baseline), str(current)],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "| benchmark | baseline (µs) | current (µs) | Δ |" in result.stdout
    pytest.warns  # silence ruff F401 if any test imports above bypass linting
