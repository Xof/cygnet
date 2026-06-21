# _summary.py — Render a benchmark JSON file as a markdown table.
#
# Used by the CI bench job to populate $GITHUB_STEP_SUMMARY.  Kept as
# a standalone script (rather than inline `python -c` in the workflow)
# so it can have proper indentation without YAML block-scalar
# whitespace traps.
#
# Usage: python -m bench._summary path/to/bench-result.json

from __future__ import annotations

import json
import sys


def render(json_path: str) -> str:
    with open(json_path) as f:
        data = json.load(f)
    lines = [
        "| benchmark | median (µs) | min (µs) | ops/s |",
        "|---|---:|---:|---:|",
    ]
    # .get with a default: a malformed/empty JSON (no "benchmarks" key) renders
    # a header-only table rather than raising KeyError in the advisory CI step.
    for b in data.get("benchmarks", []):
        med_s = b["stats"]["median"]
        med = med_s * 1e6
        mn = b["stats"]["min"] * 1e6
        # Guard the divisor: a real pytest-benchmark median is never 0, but a
        # hand-built / malformed JSON could be — emit a dash, not a crash.
        ops = f"{1 / med_s:,.0f}" if med_s else "—"
        lines.append(f"| {b['name']} | {med:,.2f} | {mn:,.2f} | {ops} |")
    return "\n".join(lines)


if __name__ == "__main__":
    print(render(sys.argv[1]))
