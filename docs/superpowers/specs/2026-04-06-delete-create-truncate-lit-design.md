# Storm: DELETE, create(), TRUNCATE, lit(), and predicate requirement

**Date:** 2026-04-06
**Status:** Approved

## Overview

Five features that fill in Storm's basic SQL surface area:

1. **`storm.all` sentinel + predicate requirement** — safety rail for UPDATE and DELETE
2. **`storm.lit()`** — raw SQL escape hatch in WHERE clauses
3. **DELETE builder** — `storm.DELETE(db).FROM(T).WHERE(...)`
4. **`storm.create(db, obj)`** — INSERT without ON CONFLICT
5. **`storm.TRUNCATE(db, *tables, cascade=False)`** — truncate one or more tables

## Implementation order

Layered bottom-up: predicate/sentinel layer first, then DELETE builder, then convenience functions. Each layer builds on tested foundations.

---

## 1. `storm.all` sentinel and predicate requirement

### Design

`_All` is a small class in `predicate.py`. `all` is a module-level instance, exported from `__init__.py`.

```python
# predicate.py
class _All:
    """Sentinel: pass to WHERE() to explicitly allow unrestricted DELETE/UPDATE."""
    pass

all = _All()
```

### Predicate requirement

Enforced at execution time in `Executor`:

- `run_update()` and `run_delete()` check `builder._predicates`.
- If empty: raise `ValueError("UPDATE/DELETE requires a WHERE clause; use WHERE(storm.all) to affect all rows")`.
- If the sole entry is `storm.all`: omit the WHERE clause entirely from rendered SQL.
- `storm.all` mixed with real predicates: raise `ValueError("storm.all cannot be combined with other predicates")`.

### Type changes

`_Builder.WHERE()` accepts `Predicate | Literal | _All`.

### Breaking change

Existing `UPDATE` calls without `.WHERE()` will now raise. This is intentional — Storm has no external users.

### SELECT behavior

`storm.all` is accepted in `SELECT(...).WHERE(storm.all)` but not required. SELECT without WHERE continues to work as before.

---

## 2. `storm.lit()`

### Design

`Literal` is a frozen dataclass in `predicate.py`:

```python
@dataclass(frozen=True)
class Literal:
    sql: str
```

`storm.lit(sql: str) -> Literal` is a factory function in `__init__.py`.

### Phase 1 scope (this spec)

`Literal` is accepted only in `.WHERE()`. When the executor renders the WHERE clause, it checks each entry:

- `Predicate`: render with parameter substitution as usual.
- `Literal`: emit `.sql` verbatim, no parameters.
- `_All`: skip WHERE clause entirely.

Multiple WHERE entries (any mix of `Predicate` and `Literal`) are AND-joined and parenthesized, same as today.

### Phase 2 (future)

`Literal` will be accepted in SELECT lists, SET clauses, and GROUP BY once the expression protocol is built.

### No parameter interpolation

`lit()` is raw SQL. No escaping, no `$N` substitution. The user is responsible for safety.

---

## 3. DELETE builder

### `DeleteBuilder` in `builders.py`

```python
class DeleteBuilder(_Builder):
    _table: TableProxy | None

    def __init__(self, db: Any) -> None:
        super().__init__(db)
        self._table = None

    def FROM(self, table: TableProxy) -> DeleteBuilder:
        self._table = table
        return self

    def __await__(self) -> Generator[Any, None, None]:
        return self._execute().__await__()

    async def _execute(self) -> None:
        from .executor import Executor
        await Executor(self._db).run_delete(self)
```

### `Executor.run_delete()` in `executor.py`

1. Check predicate requirement (same logic as `run_update`).
2. Render `DELETE FROM {table_name}`.
3. If predicates are present (and not `storm.all`): render WHERE clause with AND-joined predicates, `$N` parameter numbering.
4. Call `db.execute(sql, params)`.
5. Return `None`.

### Public API

`DELETE(db) -> DeleteBuilder` in `__init__.py`, added to `__all__`.

### Usage

```python
await storm.DELETE(db).FROM(AccountTable).WHERE(AccountTable.id == 5)
await storm.DELETE(db).FROM(AccountTable).WHERE(storm.all)
```

---

## 4. `storm.create(db, obj)`

### Design

Async function in `__init__.py`, delegates to `Executor.run_create()`.

`run_create()` is the INSERT path from `run_save()` without ON CONFLICT:

1. Introspect via `TableMeta(type(obj))`.
2. Build `INSERT INTO {table} ({cols}) VALUES ($1, $2, ...)`.
3. DBKey with `None` value: skip that column from INSERT, append `RETURNING {pk_col}`, call `execute_one()`, populate PK on the object.
4. AppKey with `None` value: raise `ValueError`.
5. No ON CONFLICT — duplicate key raises from the database.
6. Return the object.

### Usage

```python
account = await storm.create(db, Account(id=None, name="Fred", email="f@x.com"))
# account.id is now populated by the database
```

### Distinction from `save()`

| | `save()` | `create()` |
|---|---|---|
| Duplicate key | Upserts (ON CONFLICT DO UPDATE) | Raises (database constraint error) |
| Intent | "Ensure this row exists with these values" | "This row must be new" |

---

## 5. `storm.TRUNCATE()`

### Design

Standalone async function in `__init__.py`. No builder, no executor.

```python
async def TRUNCATE(db: Any, *tables: TableProxy, cascade: bool = False) -> None:
    if not tables:
        raise ValueError("TRUNCATE requires at least one table")
    names = ", ".join(t._meta.table_name for t in tables)
    sql = f"TRUNCATE TABLE {names}"
    if cascade:
        sql += " CASCADE"
    await db.execute(sql)
```

### Usage

```python
await storm.TRUNCATE(db, AccountTable)
await storm.TRUNCATE(db, AccountTable, LogEntryTable, cascade=True)
```

---

## Testing strategy

All tests follow existing patterns: async methods in test classes, `FakeDB` for unit tests.

### Unit tests (no database)

**Predicate requirement:**
- `UPDATE` without WHERE raises `ValueError`
- `DELETE` without WHERE raises `ValueError`
- `UPDATE` with `WHERE(storm.all)` renders SQL without WHERE clause
- `DELETE` with `WHERE(storm.all)` renders SQL without WHERE clause
- `storm.all` combined with other predicates raises `ValueError`
- `SELECT` without WHERE still works (no requirement)
- `SELECT` with `WHERE(storm.all)` works (accepted but not required)

**`lit()`:**
- `WHERE(storm.lit('id > 10'))` renders verbatim in SQL
- `lit()` combined with regular predicates AND-joins correctly
- `lit()` does not add parameters

**DELETE builder:**
- `DELETE FROM table WHERE col = $1` — correct SQL and params
- Chained WHERE: `.WHERE(a).WHERE(b)` renders as AND
- `DELETE FROM table` with `WHERE(storm.all)` — no WHERE clause

**`create()`:**
- DBKey with None: INSERT with RETURNING, PK populated
- AppKey with None: raises `ValueError`
- AppKey with value: INSERT without RETURNING
- No ON CONFLICT in generated SQL

**`TRUNCATE()`:**
- Single table: `TRUNCATE TABLE name`
- Multiple tables: `TRUNCATE TABLE name1, name2`
- `cascade=True`: appends CASCADE
- No tables: raises `ValueError`

### Integration tests (real PostgreSQL)

- DELETE removes rows, verified by subsequent SELECT
- `create()` inserts and returns object with PK
- `create()` on duplicate key raises
- `TRUNCATE` empties table, CASCADE drops dependent rows
- `lit()` in WHERE filters correctly
- Predicate requirement: UPDATE/DELETE without WHERE raises before hitting DB
