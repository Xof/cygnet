---
id: 0008
title: A3. Verbs are factory functions, not classes
date: 2026-04-29
status: Accepted
summary: Query verbs are factory functions in __init__.py, so builder class names never appear in user code.
---

# 0008. A3. Verbs are factory functions, not classes

`SELECT`, `INSERT`, `UPDATE`, `DELETE` are functions defined in `__init__.py` that construct builders. `TRUNCATE` is itself an async function (no builder — nothing to chain). Users never see the builder class names in their code.

**Why:** Exposing the class names would create a second way to start a query (`SelectBuilder(db, ...)` vs `cygnet.SELECT(db, ...)`). Constructor-as-API is awkwardly indirect; factory-as-API reads as a SQL verb.
