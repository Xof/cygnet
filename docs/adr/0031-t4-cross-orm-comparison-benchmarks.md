---
id: 0031
title: T4. Cross-ORM comparison benchmarks
date: 2026-04-29
status: Accepted
summary: The same workload is benchmarked through Cygnet, SQLAlchemy 2, and Django so that 'fast' has a referent.
---

# 0031. T4. Cross-ORM comparison benchmarks

`bench/comparison/test_comparison.py` runs the same workload through Cygnet, SQLAlchemy 2 (async session), and Django (sync ORM) against the same PG schema. Each ORM is benchmarked in its idiomatic mode so the deltas reflect what real applications see. SQLAlchemy's pool is clamped to one connection to match Cygnet's PsycopgDB and Django's per-request pattern.

**Why:** Absolute numbers in isolation are meaningless ("Cygnet is fast" — relative to what?). Cross-ORM comparison gives context: where Cygnet's deliberate scope-narrowness pays off, and where it gives up nothing.
