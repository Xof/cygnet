# DELETE, create(), TRUNCATE, lit(), and predicate requirement — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add five features to Storm: `storm.all` predicate sentinel, `storm.lit()` for raw SQL in WHERE, `DeleteBuilder`, `storm.create()`, and `storm.TRUNCATE()`.

**Architecture:** Layered bottom-up. Layer 1 adds `_All` sentinel and `Literal` to `predicate.py`, updates executor to enforce predicate requirements and handle literals. Layer 2 adds `DeleteBuilder`. Layer 3 adds `create()` and `TRUNCATE()` convenience functions. Each layer builds on the previous.

**Tech Stack:** Python 3.12+, asyncio, pytest (asyncio_mode=auto), ruff, mypy strict

---

## File Structure

| File | Action | Responsibility |
|---|---|---|
| `storm/predicate.py` | Modify | Add `_All` class, `all` singleton, `Literal` dataclass |
| `storm/builders.py` | Modify | Add `DeleteBuilder`, widen `WHERE()` type hint on `_Builder` |
| `storm/executor.py` | Modify | Add `run_delete()`, `run_create()`, predicate requirement checks, literal rendering |
| `storm/__init__.py` | Modify | Export `DELETE`, `create`, `TRUNCATE`, `all`, `lit` |
| `tests/test_builders.py` | Modify | Add tests for DELETE, create, TRUNCATE, lit, predicate requirement; update existing UPDATE test |
| `tests/conftest.py` | No change | Existing `FakeDB`, models, and table proxies are sufficient |

---

### Task 1: `_All` sentinel and `Literal` in `predicate.py`

**Files:**
- Modify: `storm/predicate.py`
- Test: `tests/test_builders.py`

- [ ] **Step 1: Write failing tests for predicate requirement on UPDATE**

Add to `tests/test_builders.py`. These tests assert that UPDATE without WHERE raises, and that `storm.all` suppresses the WHERE clause. Replace the existing test that asserts the opposite behavior.

First, add `storm.all` to the imports — it doesn't exist yet, so we'll also need to remove the import temporarily or add a test that references it directly. Actually, since `storm.all` will be a module attribute, the import of `storm` is sufficient.

In `tests/test_builders.py`, **replace** the existing test `test_update_no_where_affects_all_rows` (lines 245-249) with:

```python
    async def test_update_no_where_raises(self):
        """UPDATE with no WHERE must raise ValueError."""
        db = FakeDB()
        with pytest.raises(ValueError, match="requires a WHERE clause"):
            await storm.UPDATE(db).SET(AccountTable, name="x")

    async def test_update_with_all_skips_where(self):
        """UPDATE with WHERE(storm.all) generates SQL without WHERE clause."""
        db = FakeDB()
        await storm.UPDATE(db).SET(AccountTable, name="x").WHERE(storm.all)
        assert "WHERE" not in db.last_sql
        assert "UPDATE accounts SET" in db.last_sql

    async def test_update_all_mixed_with_predicates_raises(self):
        """storm.all combined with other predicates raises ValueError."""
        db = FakeDB()
        with pytest.raises(ValueError, match="cannot be combined"):
            await (
                storm.UPDATE(db)
                .SET(AccountTable, name="x")
                .WHERE(AccountTable.id == 1)
                .WHERE(storm.all)
            )
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/xof/Documents/Dev/storm && python -m pytest tests/test_builders.py::TestUpdateSQL -v`

Expected: Failures — `storm.all` doesn't exist, no `ValueError` raised on empty predicates.

- [ ] **Step 3: Add `_All`, `all`, and `Literal` to `predicate.py`**

Add the following at the end of `storm/predicate.py`:

```python
class _All:
    """Sentinel: pass to WHERE() to explicitly allow unrestricted DELETE/UPDATE."""

    pass


all = _All()


@dataclass(frozen=True)
class Literal:
    """Raw SQL fragment for use in WHERE clauses. No parameter substitution."""

    sql: str
```

- [ ] **Step 4: Update `_Builder.WHERE()` type hint in `builders.py`**

In `storm/builders.py`, update the import from `predicate`:

```python
from .predicate import Literal, Predicate, _All
```

Update `_Builder.WHERE()` signature and the `_predicates` list type:

```python
class _Builder:
    def __init__(self, db: Any) -> None:
        self._db = db
        self._predicates: list[Predicate | Literal | _All] = []

    def WHERE(self, predicate: Predicate | Literal | _All) -> _Builder:
        self._predicates.append(predicate)
        return self
```

Also update `SelectBuilder.WHERE()` and `UpdateBuilder.WHERE()` return-type overrides to accept the same union:

```python
# In SelectBuilder:
def WHERE(self, predicate: Predicate | Literal | _All) -> SelectBuilder:
    self._predicates.append(predicate)
    return self

# In UpdateBuilder:
def WHERE(self, predicate: Predicate | Literal | _All) -> UpdateBuilder:
    self._predicates.append(predicate)
    return self
```

- [ ] **Step 5: Add predicate requirement enforcement and literal rendering in `executor.py`**

In `storm/executor.py`, add an import:

```python
from .predicate import Literal, _All
```

Add a private helper method to `Executor`:

```python
def _check_predicates(
    self, predicates: list[Any], verb: str
) -> list[Any]:
    """Validate and filter predicates for UPDATE/DELETE.

    Returns the list of real predicates to render, or an empty list
    if storm.all was used (meaning: omit the WHERE clause).
    Raises ValueError if no predicates at all, or if storm.all is
    mixed with other predicates.
    """
    has_all = any(isinstance(p, _All) for p in predicates)
    real = [p for p in predicates if not isinstance(p, _All)]
    if not predicates:
        raise ValueError(
            f"{verb} requires a WHERE clause; "
            f"use WHERE(storm.all) to affect all rows"
        )
    if has_all and real:
        raise ValueError("storm.all cannot be combined with other predicates")
    if has_all:
        return []
    return real
```

Add a helper to render a WHERE clause from a mixed list of `Predicate` and `Literal`:

```python
def _render_where(self, predicates: list[Any], params: list[Any]) -> str:
    """Render a WHERE clause from a list of Predicate and Literal objects."""
    parts: list[str] = []
    for p in predicates:
        if isinstance(p, Literal):
            parts.append(f"({p.sql})")
        else:
            parts.append(f"({p.render(params)})")
    return " AND ".join(parts)
```

Update `run_update()` to enforce the predicate requirement. Replace lines 164-168 (the WHERE rendering block):

```python
        # Before: unconditionally checked b._predicates
        # After: enforce predicate requirement
        checked = self._check_predicates(b._predicates, "UPDATE")
        if checked:
            where = self._render_where(checked, params)
            sql += f" WHERE {where}"
```

Update `_render_select()` to handle `Literal` in WHERE (lines 40-42):

```python
        if b._predicates:
            where = self._render_where(b._predicates, params)
            sql += f" WHERE {where}"
```

Note: SELECT does NOT call `_check_predicates` — it has no predicate requirement.

- [ ] **Step 6: Export `all` and `lit` from `__init__.py`**

In `storm/__init__.py`, add imports:

```python
from .predicate import Literal, _All
from .predicate import all
```

Add factory function:

```python
def lit(sql: str) -> Literal:
    """Create a raw SQL literal for use in WHERE clauses."""
    return Literal(sql=sql)
```

Add to `__all__`:

```python
"all",
"lit",
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `cd /Users/xof/Documents/Dev/storm && python -m pytest tests/test_builders.py::TestUpdateSQL -v`

Expected: All UPDATE tests pass, including the three new ones.

- [ ] **Step 8: Write and run tests for `lit()` in SELECT**

Add a new test class to `tests/test_builders.py`:

```python
class TestLiteralSQL:
    async def test_lit_in_where(self):
        db = FakeDB(rows=[])
        await storm.SELECT(db).FROM(AccountTable).WHERE(storm.lit("id > 10"))
        assert db.last_sql == (
            "SELECT accounts.* FROM accounts WHERE (id > 10)"
        )
        assert db.last_params == []

    async def test_lit_combined_with_predicate(self):
        db = FakeDB(rows=[])
        await (
            storm.SELECT(db)
            .FROM(AccountTable)
            .WHERE(AccountTable.name == "Fred")
            .WHERE(storm.lit("email IS NOT NULL"))
        )
        assert db.last_sql == (
            "SELECT accounts.* FROM accounts "
            "WHERE (accounts.name = $1) AND (email IS NOT NULL)"
        )
        assert db.last_params == ["Fred"]
```

Run: `cd /Users/xof/Documents/Dev/storm && python -m pytest tests/test_builders.py::TestLiteralSQL -v`

Expected: PASS

- [ ] **Step 9: Write and run tests for `storm.all` on SELECT**

Add to the `TestSelectSQL` class in `tests/test_builders.py`:

```python
    async def test_select_with_all(self):
        """SELECT with WHERE(storm.all) works — just omits WHERE clause."""
        db = FakeDB(rows=[])
        await storm.SELECT(db).FROM(AccountTable).WHERE(storm.all)
        assert db.last_sql == "SELECT accounts.* FROM accounts"
        assert db.last_params == []
```

Run: `cd /Users/xof/Documents/Dev/storm && python -m pytest tests/test_builders.py::TestSelectSQL::test_select_with_all -v`

Expected: PASS — SELECT has no predicate requirement, and `storm.all` in the predicate list should result in no WHERE clause. However, currently `_render_select` doesn't filter out `_All` sentinels. We need to handle this.

If this test fails: update `_render_select()` in executor.py to filter out `_All` from predicates before rendering:

```python
        real_predicates = [p for p in b._predicates if not isinstance(p, _All)]
        if real_predicates:
            where = self._render_where(real_predicates, params)
            sql += f" WHERE {where}"
```

Re-run and confirm PASS.

- [ ] **Step 10: Run full test suite and lint**

Run: `cd /Users/xof/Documents/Dev/storm && python -m pytest tests/ -v --ignore=tests/integration && ruff check storm tests && ruff format --check storm tests && mypy storm`

Expected: All pass. Fix any issues.

- [ ] **Step 11: Commit**

```bash
cd /Users/xof/Documents/Dev/storm
git add storm/predicate.py storm/builders.py storm/executor.py storm/__init__.py tests/test_builders.py
git commit -m "Add storm.all predicate sentinel, lit() for raw SQL, and predicate requirement on UPDATE"
```

---

### Task 2: DELETE builder

**Files:**
- Modify: `storm/builders.py`
- Modify: `storm/executor.py`
- Modify: `storm/__init__.py`
- Test: `tests/test_builders.py`

- [ ] **Step 1: Write failing tests for DELETE**

Add a new test class to `tests/test_builders.py`:

```python
class TestDeleteSQL:
    async def test_delete_with_where(self):
        db = FakeDB()
        await storm.DELETE(db).FROM(AccountTable).WHERE(AccountTable.id == 1)
        assert db.last_sql == "DELETE FROM accounts WHERE (accounts.id = $1)"
        assert db.last_params == [1]

    async def test_delete_multiple_where(self):
        db = FakeDB()
        await (
            storm.DELETE(db)
            .FROM(AccountTable)
            .WHERE(AccountTable.name == "Fred")
            .WHERE(AccountTable.id > 5)
        )
        assert db.last_sql == (
            "DELETE FROM accounts "
            "WHERE (accounts.name = $1) AND (accounts.id > $2)"
        )
        assert db.last_params == ["Fred", 5]

    async def test_delete_with_all(self):
        db = FakeDB()
        await storm.DELETE(db).FROM(AccountTable).WHERE(storm.all)
        assert db.last_sql == "DELETE FROM accounts"
        assert db.last_params == []

    async def test_delete_no_where_raises(self):
        db = FakeDB()
        with pytest.raises(ValueError, match="requires a WHERE clause"):
            await storm.DELETE(db).FROM(AccountTable)

    async def test_delete_all_mixed_with_predicates_raises(self):
        db = FakeDB()
        with pytest.raises(ValueError, match="cannot be combined"):
            await (
                storm.DELETE(db)
                .FROM(AccountTable)
                .WHERE(AccountTable.id == 1)
                .WHERE(storm.all)
            )

    async def test_delete_with_lit(self):
        db = FakeDB()
        await storm.DELETE(db).FROM(AccountTable).WHERE(storm.lit("id > 10"))
        assert db.last_sql == "DELETE FROM accounts WHERE (id > 10)"
        assert db.last_params == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/xof/Documents/Dev/storm && python -m pytest tests/test_builders.py::TestDeleteSQL -v`

Expected: Failures — `storm.DELETE` doesn't exist.

- [ ] **Step 3: Add `DeleteBuilder` to `builders.py`**

Add the following class after `UpdateBuilder` in `storm/builders.py`:

```python
class DeleteBuilder(_Builder):
    def __init__(self, db: Any) -> None:
        super().__init__(db)
        self._table: TableProxy | None = None

    def FROM(self, table: TableProxy) -> DeleteBuilder:
        self._table = table
        return self

    def WHERE(self, predicate: Predicate | Literal | _All) -> DeleteBuilder:
        self._predicates.append(predicate)
        return self

    def __await__(self) -> Generator[Any, None, None]:
        return self._execute().__await__()

    async def _execute(self) -> None:
        from .executor import Executor

        await Executor(self._db).run_delete(self)
```

- [ ] **Step 4: Add `run_delete()` to `executor.py`**

Add the following method to the `Executor` class in `storm/executor.py`, after `run_update()`:

```python
    # ── DELETE ────────────────────────────────────────────────────────────────

    async def run_delete(self, b: Any) -> None:
        params: list[Any] = []
        meta = b._table._meta

        checked = self._check_predicates(b._predicates, "DELETE")

        sql = f"DELETE FROM {meta.table_name}"

        if checked:
            where = self._render_where(checked, params)
            sql += f" WHERE {where}"

        await self._db.execute(sql, params)
```

- [ ] **Step 5: Add `DELETE` to `__init__.py`**

In `storm/__init__.py`, update the import from builders:

```python
from .builders import DeleteBuilder, InsertBuilder, SelectBuilder, UpdateBuilder
```

Add the function:

```python
def DELETE(db: Any) -> DeleteBuilder:  # noqa: N802
    return DeleteBuilder(db)
```

Add `"DELETE"` to `__all__`.

- [ ] **Step 6: Run tests to verify they pass**

Run: `cd /Users/xof/Documents/Dev/storm && python -m pytest tests/test_builders.py::TestDeleteSQL -v`

Expected: All 6 DELETE tests pass.

- [ ] **Step 7: Run full test suite and lint**

Run: `cd /Users/xof/Documents/Dev/storm && python -m pytest tests/ -v --ignore=tests/integration && ruff check storm tests && ruff format --check storm tests && mypy storm`

Expected: All pass.

- [ ] **Step 8: Commit**

```bash
cd /Users/xof/Documents/Dev/storm
git add storm/builders.py storm/executor.py storm/__init__.py tests/test_builders.py
git commit -m "Add DELETE builder with predicate requirement"
```

---

### Task 3: `storm.create()`

**Files:**
- Modify: `storm/executor.py`
- Modify: `storm/__init__.py`
- Test: `tests/test_builders.py`

- [ ] **Step 1: Write failing tests for `create()`**

Add a new test class to `tests/test_builders.py`:

```python
class TestCreateSQL:
    async def test_create_dbkey(self):
        """create() with DBKey inserts with RETURNING, populates PK."""
        db = FakeDB(rows=[(42,)])
        acc = Account(id=None, name="Fred", email="fred@example.com")
        result = await storm.create(db, acc)
        assert "INSERT INTO" in db.last_sql
        assert "RETURNING id" in db.last_sql
        assert "ON CONFLICT" not in db.last_sql
        assert result.id == 42
        assert result is acc

    async def test_create_appkey(self):
        """create() with AppKey inserts without RETURNING."""
        db = FakeDB(rows=[])
        ev = Event(id="evt-123", name="Launch")
        result = await storm.create(db, ev)
        assert "INSERT INTO" in db.last_sql
        assert "RETURNING" not in db.last_sql
        assert "ON CONFLICT" not in db.last_sql
        assert "evt-123" in db.last_params
        assert result is ev

    async def test_create_appkey_none_raises(self):
        """create() with AppKey and None value raises ValueError."""
        db = FakeDB()
        ev = Event(id=None, name="Launch")
        with pytest.raises(ValueError, match="AppKey"):
            await storm.create(db, ev)

    async def test_create_no_on_conflict(self):
        """create() must never generate ON CONFLICT."""
        db = FakeDB(rows=[(1,)])
        acc = Account(id=None, name="Fred", email="fred@example.com")
        await storm.create(db, acc)
        assert "ON CONFLICT" not in db.last_sql
        assert "EXCLUDED" not in db.last_sql
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/xof/Documents/Dev/storm && python -m pytest tests/test_builders.py::TestCreateSQL -v`

Expected: Failures — `storm.create` doesn't exist.

- [ ] **Step 3: Add `run_create()` to `executor.py`**

Add the following method to the `Executor` class, after `run_insert()`:

```python
    # ── CREATE (INSERT, no upsert) ───────────────────────────────────────────

    async def run_create(self, obj: Any) -> Any:
        """INSERT without ON CONFLICT. Returns the object with PK populated."""
        from .proxy import TableProxy

        meta = TableProxy(type(obj))._meta
        params: list[Any] = []
        kwargs = {f.attr_name: getattr(obj, f.attr_name) for f in meta.fields}
        columns, _values = self._extract_insert_fields(meta, kwargs, params)

        col_sql = ", ".join(columns)
        val_sql = ", ".join(f"${i + 1}" for i in range(len(columns)))
        sql = f"INSERT INTO {meta.table_name} ({col_sql}) VALUES ({val_sql})"

        if meta.pk and meta.pk.primary_key == DBKey:
            sql += f" RETURNING {meta.pk.column_name}"
            row = await self._db.execute_one(sql, params)
            if row is not None:
                setattr(obj, meta.pk.attr_name, row[0])
        else:
            await self._db.execute(sql, params)

        return obj
```

- [ ] **Step 4: Add `create()` to `__init__.py`**

Add the function:

```python
async def create(db: Any, obj: Any) -> Any:
    """
    INSERT obj into its table. No ON CONFLICT — duplicates raise from the DB.

    Returns the object with PK populated (for DBKey).
    """
    from .executor import Executor

    return await Executor(db).run_create(obj)
```

Add `"create"` to `__all__`.

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd /Users/xof/Documents/Dev/storm && python -m pytest tests/test_builders.py::TestCreateSQL -v`

Expected: All 4 tests pass.

- [ ] **Step 6: Run full test suite and lint**

Run: `cd /Users/xof/Documents/Dev/storm && python -m pytest tests/ -v --ignore=tests/integration && ruff check storm tests && ruff format --check storm tests && mypy storm`

Expected: All pass.

- [ ] **Step 7: Commit**

```bash
cd /Users/xof/Documents/Dev/storm
git add storm/executor.py storm/__init__.py tests/test_builders.py
git commit -m "Add storm.create() — INSERT without ON CONFLICT"
```

---

### Task 4: `storm.TRUNCATE()`

**Files:**
- Modify: `storm/__init__.py`
- Test: `tests/test_builders.py`

- [ ] **Step 1: Write failing tests for TRUNCATE**

Add a new test class to `tests/test_builders.py`:

```python
class TestTruncateSQL:
    async def test_truncate_single_table(self):
        db = FakeDB()
        await storm.TRUNCATE(db, AccountTable)
        assert db.last_sql == "TRUNCATE TABLE accounts"

    async def test_truncate_multiple_tables(self):
        db = FakeDB()
        await storm.TRUNCATE(db, AccountTable, LogTable)
        assert db.last_sql == "TRUNCATE TABLE accounts, log_entries"

    async def test_truncate_cascade(self):
        db = FakeDB()
        await storm.TRUNCATE(db, AccountTable, cascade=True)
        assert db.last_sql == "TRUNCATE TABLE accounts CASCADE"

    async def test_truncate_multiple_cascade(self):
        db = FakeDB()
        await storm.TRUNCATE(db, AccountTable, LogTable, cascade=True)
        assert db.last_sql == "TRUNCATE TABLE accounts, log_entries CASCADE"

    async def test_truncate_no_tables_raises(self):
        db = FakeDB()
        with pytest.raises(ValueError, match="at least one table"):
            await storm.TRUNCATE(db)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/xof/Documents/Dev/storm && python -m pytest tests/test_builders.py::TestTruncateSQL -v`

Expected: Failures — `storm.TRUNCATE` doesn't exist.

- [ ] **Step 3: Add `TRUNCATE()` to `__init__.py`**

Add the function:

```python
async def TRUNCATE(db: Any, *tables: TableProxy, cascade: bool = False) -> None:  # noqa: N802
    """Truncate one or more tables. Use cascade=True to drop dependent rows."""
    if not tables:
        raise ValueError("TRUNCATE requires at least one table")
    names = ", ".join(t._meta.table_name for t in tables)
    sql = f"TRUNCATE TABLE {names}"
    if cascade:
        sql += " CASCADE"
    await db.execute(sql)
```

Add `"TRUNCATE"` to `__all__`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/xof/Documents/Dev/storm && python -m pytest tests/test_builders.py::TestTruncateSQL -v`

Expected: All 5 tests pass.

- [ ] **Step 5: Run full test suite and lint**

Run: `cd /Users/xof/Documents/Dev/storm && python -m pytest tests/ -v --ignore=tests/integration && ruff check storm tests && ruff format --check storm tests && mypy storm`

Expected: All pass.

- [ ] **Step 6: Commit**

```bash
cd /Users/xof/Documents/Dev/storm
git add storm/__init__.py tests/test_builders.py
git commit -m "Add storm.TRUNCATE() for one or more tables"
```

---

### Task 5: Update `CLAUDE.md` and final verification

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Update CLAUDE.md**

Add the new features to the "Key patterns" section. After the bullet about `$N parameter style`, add:

```markdown
- **Predicate requirement.** `DELETE` and `UPDATE` must have a `.WHERE()` clause. Use `WHERE(storm.all)` to explicitly affect all rows. `SELECT` has no such requirement.
- **`storm.lit(sql)`** — raw SQL literal for WHERE clauses. No parameter substitution. Use when Storm's predicate API doesn't cover your case.
- **`storm.create(db, obj)`** — INSERT without ON CONFLICT. Duplicate keys raise from the database. Returns the object with PK populated.
- **`storm.TRUNCATE(db, *tables, cascade=False)`** — truncates one or more tables. No builder needed.
```

Update the "Architecture" section to mention DELETE:

```
builders.py    — SelectBuilder, InsertBuilder, UpdateBuilder, DeleteBuilder (fluent, awaitable)
```

Add to "Gotchas":

```markdown
- `UPDATE` and `DELETE` without `.WHERE()` raise `ValueError` — use `WHERE(storm.all)` to affect all rows intentionally.
```

- [ ] **Step 2: Run full test suite one final time**

Run: `cd /Users/xof/Documents/Dev/storm && python -m pytest tests/ -v --ignore=tests/integration && ruff check storm tests && ruff format --check storm tests && mypy storm`

Expected: All pass.

- [ ] **Step 3: Commit**

```bash
cd /Users/xof/Documents/Dev/storm
git add CLAUDE.md
git commit -m "Update CLAUDE.md with DELETE, create, TRUNCATE, lit, predicate requirement"
```
