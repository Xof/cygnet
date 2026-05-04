# Builder `.sql()` Method Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `.sql()` to all four query builders so users can inspect generated SQL without executing it.

**Architecture:** Extract rendering logic from Executor's `run_*` methods into public `render_*` methods that return `(sql, params)`. Each builder's `.sql()` is a one-liner that calls the corresponding render method. `run_*` methods are refactored to call `render_*` then execute.

**Tech Stack:** Python 3.12+, mypy strict, ruff, pytest with asyncio_mode=auto

---

### Task 1: Extract render_select and add SelectBuilder.sql()

**Files:**
- Modify: `storm/executor.py`
- Modify: `storm/builders.py`
- Test: `tests/test_builders.py`

- [ ] **Step 1: Write the test for SelectBuilder.sql()**

Add a new test class to `tests/test_builders.py`:

```python
class TestBuilderSQL:
    def test_select_sql(self):
        db = FakeDB()
        sql, params = (
            storm.SELECT(db).FROM(AccountTable).WHERE(AccountTable.id == 1).sql()
        )
        assert sql == "SELECT accounts.* FROM accounts WHERE (accounts.id = $1)"
        assert params == [1]

    def test_select_sql_full_chain(self):
        db = FakeDB()
        sql, params = (
            storm.SELECT(db, AccountTable.name)
            .FROM(AccountTable)
            .JOIN(LogTable, ON=AccountTable.id == LogTable.account_id)
            .WHERE(AccountTable.name == "Fred")
            .GROUP_BY(AccountTable.name)
            .ORDER_BY(AccountTable.name)
            .LIMIT(10)
            .sql()
        )
        assert "SELECT accounts.name FROM accounts" in sql
        assert "INNER JOIN log_entries ON" in sql
        assert "WHERE" in sql
        assert "GROUP BY accounts.name" in sql
        assert "ORDER BY accounts.name ASC" in sql
        assert "LIMIT 10" in sql
        assert params == ["Fred"]
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/test_builders.py::TestBuilderSQL -v`
Expected: FAIL — `SelectBuilder` has no `sql` method.

- [ ] **Step 3: Add render_select to Executor**

In `storm/executor.py`, add this method to the `Executor` class, right before the `run_select` method:

```python
    def render_select(self, b: Any) -> tuple[str, list[Any]]:
        params: list[Any] = []
        sql = self._render_select(b, params)
        return sql, params
```

Then refactor `run_select` to use it:

```python
    async def run_select(self, b: Any) -> list[Any]:
        sql, params = self.render_select(b)
        rows = await self._db.execute(sql, params)
        return self._map_select(b, rows)
```

- [ ] **Step 4: Add sql() to SelectBuilder**

In `storm/builders.py`, add this method to `SelectBuilder`, between `LIMIT` and `__await__`:

```python
    def sql(self) -> tuple[str, list[Any]]:
        from .executor import Executor

        return Executor(self._db).render_select(self)
```

- [ ] **Step 5: Run tests**

Run: `pytest tests/ --ignore=tests/integration -v`
Expected: PASS — new tests and all existing tests.

- [ ] **Step 6: Commit**

```bash
git add storm/executor.py storm/builders.py tests/test_builders.py
git commit -m "Add render_select and SelectBuilder.sql()"
```

---

### Task 2: Extract render_delete and add DeleteBuilder.sql()

**Files:**
- Modify: `storm/executor.py`
- Modify: `storm/builders.py`
- Test: `tests/test_builders.py`

- [ ] **Step 1: Write the tests**

Add to `TestBuilderSQL` in `tests/test_builders.py`:

```python
    def test_delete_sql(self):
        db = FakeDB()
        sql, params = (
            storm.DELETE(db).FROM(AccountTable).WHERE(AccountTable.id == 1).sql()
        )
        assert sql == "DELETE FROM accounts WHERE (accounts.id = $1)"
        assert params == [1]

    def test_delete_sql_no_where_raises(self):
        db = FakeDB()
        with pytest.raises(ValueError, match="requires a WHERE clause"):
            storm.DELETE(db).FROM(AccountTable).sql()

    def test_delete_sql_with_all(self):
        db = FakeDB()
        sql, params = (
            storm.DELETE(db).FROM(AccountTable).WHERE(storm.all).sql()
        )
        assert sql == "DELETE FROM accounts"
        assert params == []
```

- [ ] **Step 2: Run to verify they fail**

Run: `pytest tests/test_builders.py::TestBuilderSQL::test_delete_sql -v`
Expected: FAIL — `DeleteBuilder` has no `sql` method.

- [ ] **Step 3: Add render_delete to Executor**

In `storm/executor.py`, add this method before `run_delete`:

```python
    def render_delete(self, b: Any) -> tuple[str, list[Any]]:
        params: list[Any] = []
        meta = b._table._meta
        checked = self._check_predicates(b._predicates, "DELETE")
        sql = f"DELETE FROM {meta.table_name}"
        if checked:
            where = self._render_where(checked, params)
            sql += f" WHERE {where}"
        return sql, params
```

Then refactor `run_delete` to use it:

```python
    async def run_delete(self, b: Any) -> None:
        sql, params = self.render_delete(b)
        await self._db.execute(sql, params)
```

- [ ] **Step 4: Add sql() to DeleteBuilder**

In `storm/builders.py`, add this method to `DeleteBuilder`, between `WHERE` and `__await__`:

```python
    def sql(self) -> tuple[str, list[Any]]:
        from .executor import Executor

        return Executor(self._db).render_delete(self)
```

- [ ] **Step 5: Run tests**

Run: `pytest tests/ --ignore=tests/integration -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add storm/executor.py storm/builders.py tests/test_builders.py
git commit -m "Add render_delete and DeleteBuilder.sql()"
```

---

### Task 3: Extract render_update and add UpdateBuilder.sql()

**Files:**
- Modify: `storm/executor.py`
- Modify: `storm/builders.py`
- Test: `tests/test_builders.py`

- [ ] **Step 1: Write the tests**

Add to `TestBuilderSQL` in `tests/test_builders.py`:

```python
    def test_update_sql(self):
        db = FakeDB()
        sql, params = (
            storm.UPDATE(db)
            .SET(AccountTable, name="Wilma")
            .WHERE(AccountTable.id == 1)
            .sql()
        )
        assert "UPDATE accounts SET name = $1" in sql
        assert "WHERE (accounts.id = $2)" in sql
        assert params == ["Wilma", 1]

    def test_update_sql_no_where_raises(self):
        db = FakeDB()
        with pytest.raises(ValueError, match="requires a WHERE clause"):
            storm.UPDATE(db).SET(AccountTable, name="x").sql()

    def test_update_sql_noop(self):
        db = FakeDB()
        sql, params = storm.UPDATE(db).SET(AccountTable).sql()
        assert sql == ""
        assert params == []
```

- [ ] **Step 2: Run to verify they fail**

Run: `pytest tests/test_builders.py::TestBuilderSQL::test_update_sql -v`
Expected: FAIL — `UpdateBuilder` has no `sql` method.

- [ ] **Step 3: Add render_update to Executor**

In `storm/executor.py`, add this method before `run_update`:

```python
    def render_update(self, b: Any) -> tuple[str, list[Any]]:
        params: list[Any] = []
        meta = b._table._meta

        if b._obj is not None:
            if not isinstance(b._obj, meta.cls):
                raise TypeError(
                    f"Expected {meta.cls.__name__}, got {type(b._obj).__name__}"
                )
            kwargs = {
                f.attr_name: getattr(b._obj, f.attr_name)
                for f in meta.fields
                if f.primary_key is None
            }
        else:
            kwargs = b._kwargs

        set_clauses: list[str] = []
        for f in meta.fields:
            if f.attr_name in kwargs:
                params.append(kwargs[f.attr_name])
                set_clauses.append(f"{f.column_name} = ${len(params)}")

        if not set_clauses:
            return "", []

        sql = f"UPDATE {meta.table_name} SET {', '.join(set_clauses)}"

        checked = self._check_predicates(b._predicates, "UPDATE")
        if checked:
            where = self._render_where(checked, params)
            sql += f" WHERE {where}"

        return sql, params
```

Then refactor `run_update` to use it:

```python
    async def run_update(self, b: Any) -> None:
        sql, params = self.render_update(b)
        if not sql:
            return
        await self._db.execute(sql, params)
```

- [ ] **Step 4: Add sql() to UpdateBuilder**

In `storm/builders.py`, add this method to `UpdateBuilder`, between `SET` and `__await__`:

```python
    def sql(self) -> tuple[str, list[Any]]:
        from .executor import Executor

        return Executor(self._db).render_update(self)
```

- [ ] **Step 5: Run tests**

Run: `pytest tests/ --ignore=tests/integration -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add storm/executor.py storm/builders.py tests/test_builders.py
git commit -m "Add render_update and UpdateBuilder.sql()"
```

---

### Task 4: Extract render_insert and add InsertBuilder.sql()

**Files:**
- Modify: `storm/executor.py`
- Modify: `storm/builders.py`
- Test: `tests/test_builders.py`

- [ ] **Step 1: Write the tests**

Add to `TestBuilderSQL` in `tests/test_builders.py`:

```python
    def test_insert_sql_kwargs(self):
        db = FakeDB()
        sql, params = (
            storm.INSERT(db)
            .INTO(AccountTable)
            .VALUES(name="Fred", email="fred@example.com")
            .sql()
        )
        assert "INSERT INTO accounts" in sql
        assert "RETURNING id" in sql
        assert "Fred" in params
        assert "fred@example.com" in params

    def test_insert_sql_object(self):
        db = FakeDB()
        acc = Account(id=None, name="Fred", email="fred@example.com")
        sql, params = storm.INSERT(db).INTO(AccountTable).VALUES(acc).sql()
        assert "INSERT INTO accounts" in sql
        assert "RETURNING id" in sql
        assert "Fred" in params

    def test_insert_sql_appkey_none_raises(self):
        db = FakeDB()
        ev = Event(id=None, name="Launch")
        with pytest.raises(ValueError, match="AppKey"):
            storm.INSERT(db).INTO(EventTable).VALUES(ev).sql()

    def test_insert_sql_appkey_no_returning(self):
        db = FakeDB()
        ev = Event(id="evt-123", name="Launch")
        sql, params = storm.INSERT(db).INTO(EventTable).VALUES(ev).sql()
        assert "INSERT INTO events" in sql
        assert "RETURNING" not in sql
        assert "evt-123" in params
```

- [ ] **Step 2: Run to verify they fail**

Run: `pytest tests/test_builders.py::TestBuilderSQL::test_insert_sql_kwargs -v`
Expected: FAIL — `InsertBuilder` has no `sql` method.

- [ ] **Step 3: Add render_insert to Executor**

In `storm/executor.py`, add this method before `run_insert`:

```python
    def render_insert(self, b: Any) -> tuple[str, list[Any]]:
        params: list[Any] = []
        meta = b._table._meta
        obj, kwargs = b._obj, b._kwargs

        if obj is not None:
            kwargs = {f.attr_name: getattr(obj, f.attr_name) for f in meta.fields}

        columns, _values = self._extract_insert_fields(meta, kwargs, params)
        col_sql = ", ".join(columns)
        val_sql = ", ".join(f"${i + 1}" for i in range(len(columns)))
        sql = f"INSERT INTO {meta.table_name} ({col_sql}) VALUES ({val_sql})"

        if meta.pk and meta.pk.primary_key == DBKey:
            sql += f" RETURNING {meta.pk.column_name}"

        return sql, params
```

Then refactor `run_insert` to use it:

```python
    async def run_insert(self, b: Any) -> Any:
        sql, params = self.render_insert(b)
        meta = b._table._meta

        if meta.pk and meta.pk.primary_key == DBKey:
            row = await self._db.execute_one(sql, params)
            if b._obj is not None and row is not None:
                setattr(b._obj, meta.pk.attr_name, row[0])
            return row[0] if row else None

        await self._db.execute(sql, params)
        return None
```

- [ ] **Step 4: Add sql() to InsertBuilder**

In `storm/builders.py`, add this method to `InsertBuilder`, between `VALUES` and `__await__`:

```python
    def sql(self) -> tuple[str, list[Any]]:
        from .executor import Executor

        return Executor(self._db).render_insert(self)
```

- [ ] **Step 5: Run tests**

Run: `pytest tests/ --ignore=tests/integration -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add storm/executor.py storm/builders.py tests/test_builders.py
git commit -m "Add render_insert and InsertBuilder.sql()"
```

---

### Task 5: Update CLAUDE.md

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Add .sql() to Key patterns**

In `CLAUDE.md`, add a new bullet to the Key patterns section, after the
"Builders are awaitable" bullet:

```
- **`.sql()` on builders.** `builder.sql()` returns `(sql_string, params_list)` without executing the query. Available on all four builders (`SELECT`, `INSERT`, `UPDATE`, `DELETE`). Enforces the same validation as execution (e.g., UPDATE/DELETE require WHERE).
```

- [ ] **Step 2: Run full check**

Run: `ruff check storm tests && ruff format --check storm tests && mypy storm && pytest tests/ --ignore=tests/integration`
Expected: all pass

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md
git commit -m "Document .sql() builder method in CLAUDE.md"
```
