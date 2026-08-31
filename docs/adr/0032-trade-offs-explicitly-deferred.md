---
id: 0032
title: Trade-offs explicitly deferred
date: 2026-04-29
status: Accepted
summary: Six deliberate deferrals: IDE autocomplete, composite PKs, per-branch set-op ORDER BY, save() upsert refresh, op() factory-factory, and pre-1.0 API stability.
---

# 0032. Trade-offs explicitly deferred

These are not bugs; they're choices to revisit later or never:

1. **Per-field IDE autocomplete on `T.col`.** Mypy can't project a generic-parameter class's fields into typed proxy attributes without a plugin (SQLAlchemy 2 ships one). The runtime API is correct; static field types resolve as `ColumnProxy` (effectively `Any`). Workaround: `python -m cygnet.stubs myapp.models` generates a paste-in `TYPE_CHECKING` block. Revisit if adoption justifies a plugin.

2. **Composite primary keys.** Models must have exactly one `DBKey` or `AppKey` field; `_introspect` enforces this. Composite PKs would require widening `cygnet.get(db, T, **pk_kwargs)` to take multiple PK fields and threading composite identity through `save()`, FK validation, and `follow()`. Defer until a real use case appears.

3. **Multi-statement set-op ORDER BY positioning.** Cygnet emits ORDER BY/LIMIT/OFFSET on the compound result (PG's syntactic convention). Per-branch ORDER BY (PG allows it with parens) is not exposed.

4. **`save()` does not refresh non-PK columns on upsert.** The upsert path (`INSERT … ON CONFLICT DO UPDATE`) emits no `RETURNING`. If the table has triggers, generated columns, or `DEFAULT now()` on update, the in-memory object diverges from the row that was written. The fresh-INSERT branch *does* refresh. Documented inconsistency; revisit when someone trips on it.

5. **`cygnet.op()` 1-arg factory-factory.** `op("ILIKE")` returns a callable that constructs Predicates. Used rarely in tests. Could be deprecated in favour of users writing a trivial lambda. Defer.

6. **Pre-1.0 API stability.** Cygnet is 0.1.0. Public verbs and behaviours are stable in spirit, but the alpha label is honest — breaking changes can land if a better shape emerges. The 87→1 commit squash is a one-time act; future history is preserved.
