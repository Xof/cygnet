---
id: 0027
title: PF2. `bench/profile_hotspots.py` — deterministic + wall-clock hotspot profiler
date: 2026-06-27
status: Accepted
summary: bench/profile_hotspots.py pairs cProfile (where the time goes) with timeit (real ns/op) on FakeDB-isolated workloads.
---

# 0027. PF2. `bench/profile_hotspots.py` — deterministic + wall-clock hotspot profiler

A standalone profiler complementing the pytest-benchmark suite (T3): cProfile (*where* the time goes — self-time, call counts) plus `timeit` (the real ns/op, since cProfile's per-call hook inflates call-heavy code). It runs the render / full-path / hydration workloads FakeDB-isolated (no PG / network / asyncio) and drives `await builder` with a single `gen.send(None)` so the profile stays pure Cygnet rather than asyncio plumbing. T3 answers "did this op get slower?"; this answers "which function owns the time?". Run with `just profile`.
