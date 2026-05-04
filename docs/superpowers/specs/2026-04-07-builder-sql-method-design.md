# Builder `.sql()` Method Design

**Date:** 2026-04-07
**Status:** Approved
**Scope:** Add `.sql()` to all four query builders. Refactor Executor to
separate rendering from execution.

## Goal

Let users inspect the SQL a builder would generate without executing it.
`builder.sql()` returns `(sql_string, params_list)`.

## Decisions

| Question | Decision |
|---|---|
| Which builders? | All four: SELECT, INSERT, UPDATE, DELETE |
| RETURNING clause? | Include it — show exact SQL that would be sent |
| Require `db` arg? | Use existing `db` from builder construction (ignored) |
| Predicate enforcement? | Yes — `.sql()` raises same `ValueError` as execution |

## Executor Refactor

Extract rendering into public `render_*` methods that return
`tuple[str, list[Any]]`. The `run_*` methods call these then execute.

### render_select

```python
def render_select(self, b: Any) -> tuple[str, list[Any]]:
    params: list[Any] = []
    sql = self._render_select(b, params)
    return sql, params
```

`run_select` becomes: `render_select` → execute → map rows.

### render_delete

```python
def render_delete(self, b: Any) -> tuple[str, list[Any]]:
    params: list[Any] = []
    checked = self._check_predicates(b._predicates, "DELETE")
    sql = f"DELETE FROM {b._table._meta.table_name}"
    if checked:
        where = self._render_where(checked, params)
        sql += f" WHERE {where}"
    return sql, params
```

`run_delete` becomes: `render_delete` → execute.

### render_update

Extracted from `run_update`. Includes type check, kwargs extraction, SET
clause building, predicate check. Returns `("", [])` if no SET clauses
(matching current no-op behavior). `run_update` becomes:
`render_update` → early return on empty string → execute.

### render_insert

Extracted from `run_insert`. Includes `RETURNING` clause for DBKey models.
SQL generation only — no `setattr` on the object, no `execute`.
`run_insert` becomes: `render_insert` → execute → setattr if needed.

## Builder `.sql()` Methods

Each builder gets a one-liner `.sql()` method:

```python
def sql(self) -> tuple[str, list[Any]]:
    from .executor import Executor
    return Executor(self._db).render_select(self)
```

- Return type: `tuple[str, list[Any]]` for all four builders
- Not on `_Builder` base — each calls a different render method
- No `__init__.py` changes — `.sql()` is on builders already returned
  by `storm.SELECT()` etc.

### UPDATE edge case

`render_update` returns `("", [])` when there are no SET clauses.
`.sql()` passes this through, matching the current no-op behavior.

## Testing

All unit tests, FakeDB, no database needed.

### Contract tests

1. `SELECT(db).FROM(T).WHERE(T.id == 1).sql()` →
   `("SELECT accounts.* ... WHERE (accounts.id = $1)", [1])`
2. `INSERT(db).INTO(T).VALUES(name="Fred", email="f@e.com").sql()` →
   SQL with placeholders, `RETURNING` for DBKey
3. `UPDATE(db).SET(T, name="Wilma").WHERE(T.id == 1).sql()` →
   UPDATE with SET and WHERE
4. `DELETE(db).FROM(T).WHERE(T.id == 1).sql()` →
   DELETE with WHERE

### Predicate enforcement

5. `UPDATE(db).SET(T, name="x").sql()` → raises `ValueError`
6. `DELETE(db).FROM(T).sql()` → raises `ValueError`

### Edge cases

7. `UPDATE(db).SET(T).sql()` → `("", [])`
8. SELECT with joins, GROUP BY, ORDER BY, LIMIT — full clause chain

### Regression

All existing tests pass unchanged — `run_*` methods refactored internally
but behavior is identical.
