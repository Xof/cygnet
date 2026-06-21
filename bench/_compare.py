# _compare.py — Diff two pytest-benchmark JSON outputs as markdown.
#
# Used by the CI bench job on pull_request events: downloads main's
# latest bench-results artifact, compares each benchmark's median
# against the current run, and emits a markdown table for
# $GITHUB_STEP_SUMMARY.  Advisory-only — the comparison never fails
# the job; it just surfaces the deltas.
#
# Match key is `fullname` (e.g.
# `bench/test_render.py::TestRenderSelect::test_simple_select`),
# which uniquely identifies a benchmark across runs even when both
# files contain different subsets of tests (added / removed).
#
# Usage: python -m bench._compare baseline.json current.json

from __future__ import annotations

import json
import sys

# Threshold for highlighting a delta as a regression (rendered in bold).
# Pytest-benchmark medians on hosted CI runners typically vary ±5–10%
# round-to-round from noise alone, so a 15% threshold reliably picks
# out *real* movement without flagging every run.  Tune lower if false
# negatives become a problem; raise if false positives do.
NOISE_THRESHOLD_PCT = 15.0


def _load(path: str) -> dict[str, float]:
    """Map fullname -> median seconds.

    fullname is the file::Class::method path that pytest assigns;
    it's stable across runs for the same benchmark (unlike `name`,
    which can collide when classes have same-named methods — e.g.
    `test_cygnet` appears in TestSelectByPk, TestSelectAll, etc.).
    """
    with open(path) as f:
        data = json.load(f)
    return {b["fullname"]: b["stats"]["median"] for b in data["benchmarks"]}


def _format_delta(baseline_s: float | None, current_s: float | None) -> str:
    """Render a single benchmark's delta cell in markdown."""
    if baseline_s is None:
        return "**new**"
    if current_s is None:
        return "*removed*"
    pct = (current_s - baseline_s) / baseline_s * 100
    arrow = "↑" if pct > 0 else "↓"
    # Bold when the change exceeds NOISE_THRESHOLD_PCT and is in the
    # regression direction (current > baseline).  Improvements (negative
    # pct) get an arrow but no bold — celebrating speedups isn't the
    # point; surfacing slowdowns is.
    if pct > NOISE_THRESHOLD_PCT:
        return f"**{arrow} {pct:+.1f}%**"
    return f"{arrow} {pct:+.1f}%"


def _format_us(seconds: float | None) -> str:
    return "—" if seconds is None else f"{seconds * 1e6:,.2f}"


def render(baseline_path: str, current_path: str) -> str:
    baseline = _load(baseline_path)
    current = _load(current_path)
    # Union, sorted, so removed benchmarks still appear (with a *removed*
    # marker) and new ones get **new**.  Stable order across runs makes
    # diffs easier to scan.
    names = sorted(set(baseline) | set(current))

    # Show just the "Class::method" portion — the file path adds noise
    # without helping orient the reader.  Falls back to the full name
    # for anything that doesn't match the pattern.
    def short(name: str) -> str:
        return name.split("::", 1)[-1] if "::" in name else name

    lines = [
        "| benchmark | baseline (µs) | current (µs) | Δ |",
        "|---|---:|---:|---:|",
    ]
    for name in names:
        b = baseline.get(name)
        c = current.get(name)
        lines.append(
            f"| {short(name)} | {_format_us(b)} | {_format_us(c)} "
            f"| {_format_delta(b, c)} |"
        )
    return "\n".join(lines)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(
            "usage: python -m bench._compare baseline.json current.json",
            file=sys.stderr,
        )
        sys.exit(2)
    print(render(sys.argv[1], sys.argv[2]))
