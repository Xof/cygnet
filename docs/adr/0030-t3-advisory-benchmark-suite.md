---
id: 0030
title: T3. Advisory benchmark suite
date: 2026-04-29
status: Accepted
summary: Benchmarks run on every push but are continue-on-error, so runner noise never blocks a merge.
---

# 0030. T3. Advisory benchmark suite

`bench/` uses `pytest-benchmark` against `FakeDB` (render-only), through `FakeDB` end-to-end (overhead), and against real PG (e2e). CI runs all three on every push/PR, uploads JSON as an artifact, and posts a delta table comparing PR vs main. The job is `continue-on-error: true` — a slowdown never blocks merge.

**Why:** Performance regressions in an alpha-stage library are not merge-blocking concerns; the signal (artifact + step summary) is enough. Forcing a benchmark threshold creates false positives from runner noise and stalls PRs unnecessarily.
