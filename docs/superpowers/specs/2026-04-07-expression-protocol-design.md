# Expression Protocol Design

**Date:** 2026-04-07
**Status:** Approved
**Scope:** Internal refactor + `lit()` broadening. No new public API.

## Goal

Generalize Storm's builders and executor to accept any SQL expression — not
just `ColumnProxy` — in SELECT lists, WHERE, GROUP BY, and ORDER BY clauses.
This is the architectural prerequisite for `storm.op()` and `storm.fn()`.

## Decisions

| Question | Decision |
|---|---|
| Aliasing (`.AS()`) | Design for it later, don't implement now (option C) |
| Protocol mechanism | `typing.Protocol` — mypy strict already in use (option A) |
| Column qualification | Always fully qualified `table.col` (option A) |
| `Predicate` in protocol | Yes — `Predicate` satisfies `SQLRenderable` (option A) |
| `Literal.render_sql` signature | Accepts `params`, ignores it — uniform signature (option A) |
| Module location | New `storm/expression.py` (Approach 1) |

## The Protocol

New file `storm/expression.py`:

```python
from __future__ import annotations
from typing import Any, Protocol

class SQLRenderable(Protocol):
    def render_sql(self, params: list[Any]) -> str: ...
```

Future expression types (`FunctionExpression`, `OpExpression`) will also live
in this module.

### Implementors

- **`ColumnProxy.render_sql(params)`** — returns `"table_name.column_name"`,
  ignores `params`.
- **`Literal.render_sql(params)`** — returns `self.sql`, ignores `params`.
- **`Predicate.render_sql(params)`** — existing `render()` logic, renamed.

## Builder Changes

Type hints broaden from `ColumnProxy` / `Any` to `SQLRenderable`:

- `SelectBuilder.__init__(db, *columns)` — `columns: tuple[SQLRenderable, ...]`
- `SelectBuilder.ORDER_BY(*columns)` — same
- `SelectBuilder.GROUP_BY(*columns)` — same
- `UpdateBuilder.SET` kwargs values stay `Any` (data values, not expressions)

WHERE clauses broaden to `SQLRenderable | _All`. Since `Predicate` and
`Literal` both satisfy `SQLRenderable`, this is backward-compatible.
`_All` stays separate (sentinel, not renderable).

Builders themselves change minimally — they store expressions; rendering is
in the executor.

## Executor Changes

Six hardcoded `c._table._meta.table_name + "." + c._field.column_name`
patterns become `c.render_sql(params)`:

1. **SELECT column list** — each column in `b._columns`
2. **GROUP BY** — each column in `b._group`
3. **ORDER BY** — each column/direction pair in `b._order`. Note: when a
   `Literal` (or other non-`ColumnProxy` expression) is used in `ORDER_BY`,
   the `DESC=True` flag is ignored — the literal string controls the
   direction. The executor skips appending `ASC`/`DESC` when
   `render_sql` is used on a non-`ColumnProxy` expression, since the
   caller controls the full SQL fragment.
4. **`_render_where`** — unifies `Predicate` and `Literal` branches into
   single `p.render_sql(params)` call

What doesn't change: `_extract_insert_fields`, `run_save`, `_row_to_obj`,
`run_create` — these work with `FieldMeta` and dataclass values, not SQL
expressions.

## Predicate Internals Rework

**Current:** `Predicate.render()` checks `isinstance(self.left, Predicate)`
and `isinstance(..., ColumnProxy)` to dispatch rendering.

**New:** `Predicate.render_sql(params)` uses `hasattr(x, "render_sql")`:

- **Left side:** If it has `render_sql`, call it. For compound predicates
  (`AND`/`OR`), wrap in parens.
- **Right side:** If `AND`/`OR`, call `self.right.render_sql(params)` in
  parens. If it has `render_sql`, call it (covers `ColumnProxy`, `Literal`,
  future expressions). Otherwise it's a plain Python value — append to
  `params`, emit `$N`.

Detection uses `hasattr(x, "render_sql")` rather than `isinstance` with
`@runtime_checkable`, because structural subtyping makes `hasattr` simpler
and sufficient. The `Protocol` class is for mypy, not runtime dispatch.

Parenthesization: compound predicates (`AND`/`OR`) wrapped in parens as
today. Atomic expressions don't.

## Public API and Backward Compatibility

- **`storm.lit()`** — no signature change. `Literal` now satisfies
  `SQLRenderable`, so it works in SELECT, GROUP BY, ORDER BY — not just
  WHERE. Docstring updates accordingly.
- **`storm.Table()`** — no change.
- **No new public API.** The protocol is internal. `SQLRenderable` is not
  added to `__all__`.
- **100% backward compatible.** Every existing query produces identical SQL.
  The only observable difference is `lit()` working in more positions.

## Testing Strategy

### Unit tests (FakeDB, no database needed)

1. **`render_sql` contract tests** — verify each type returns expected SQL.
2. **`lit()` in new positions:**
   - `SELECT(db, storm.lit("COUNT(*)")).FROM(T)` → `SELECT COUNT(*) FROM ...`
   - `SELECT(db).FROM(T).ORDER_BY(storm.lit("created_at DESC"))` → `ORDER BY created_at DESC`
   - `SELECT(db, T.name).FROM(T).GROUP_BY(storm.lit("name"))` → `GROUP BY name`
3. **Mixed expressions in SELECT** — `SELECT(db, T.name, storm.lit("1 AS one")).FROM(T)`
4. **Cross-column predicates** — `WHERE(T.a == T.b)` still works.
5. **All existing tests pass unchanged.**

### Integration tests

No new integration tests. The protocol is an internal refactor; SQL output
for existing queries doesn't change. `lit()` in new positions is adequately
tested via FakeDB SQL capture.
