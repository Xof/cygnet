---
id: 0028
title: T1. `FakeDB` captures SQL + params for unit tests
date: 2026-04-29
status: Accepted
summary: Most unit tests run against FakeDB and assert on generated SQL and params rather than on PostgreSQL behaviour.
---

# 0028. T1. `FakeDB` captures SQL + params for unit tests

The vast majority of tests (389 unit tests) use `FakeDB`. It satisfies the four-method protocol, records every call in `self.calls`, and returns whatever rows the test preloaded.

**Why:** Unit tests should test Cygnet's SQL generation and result mapping, not PostgreSQL's behaviour. Asserting on the generated SQL string (`db.last_sql`) and params (`db.last_params`) catches regressions in the render path. PG-specific behaviour is verified separately in integration tests.
