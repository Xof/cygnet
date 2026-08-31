---
id: 0029
title: T2. Integration tests run against real PostgreSQL
date: 2026-04-29
status: Accepted
summary: tests/integration/ runs real round-trips across a PG 14-18 matrix for behaviour only a real planner can verify.
---

# 0029. T2. Integration tests run against real PostgreSQL

`tests/integration/` runs against a live database via psycopg. CI matrices over PG 14, 15, 16, 17, 18. Integration tests skip locally when `CYGNET_TEST_DSN` is unset.

**Why:** ON CONFLICT semantics, RETURNING shape, transaction isolation, jsonb adapter behaviour, set operation NULL semantics, and lock contention behaviour can only be verified against a real planner. The version matrix catches PG-version-specific changes (e.g., jsonpath operators added in 12, multirange types in 14).
