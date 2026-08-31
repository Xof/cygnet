---
id: 0023
title: CO1. CTE / RecursiveCTE / Lateral duck-type a `TableProxy`
date: 2026-04-29
status: Accepted
summary: CTE, RecursiveCTE, and Lateral duck-type the minimal TableProxy surface, so they compose into FROM and JOIN with no special-case render path.
---

# 0023. CO1. CTE / RecursiveCTE / Lateral duck-type a `TableProxy`

`CTE`, `RecursiveCTE`, and `Lateral` (a CTE subclass) all expose the same minimal surface the executor reads: `_sql_name`, `_meta.table_name`, `_meta.fields`, `_alias`. They stamp `ColumnProxy` attributes on themselves at construction time so `cte.colname == 5` returns a `Predicate` just like `T.colname == 5`.

**Why:** Once the executor renders against this minimal surface, CTEs and lateral joins compose into `FROM` / `JOIN` / WHERE positions without special-case rendering paths. The placement difference (WITH clause vs JOIN LATERAL) is handled by a single `isinstance(jt, Lateral)` check inside the JOIN render loop.

**Code-debt note (carried forward from review):** the duck-typing should formalise into a `TableSourceProtocol`; the current `# type: ignore` annotations on `setattr(self, col, ColumnProxy(self, field))` are the tell.
