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
    for b in data["benchmarks"]:
        med = b["stats"]["median"] * 1e6
        mn = b["stats"]["min"] * 1e6
        ops = 1 / b["stats"]["median"]
        lines.append(f"| {b['name']} | {med:,.2f} | {mn:,.2f} | {ops:,.0f} |")
    return "\n".join(lines)


if __name__ == "__main__":
    print(render(sys.argv[1]))
