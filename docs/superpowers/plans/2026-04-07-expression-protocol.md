# Expression Protocol Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Generalize Storm's builders and executor to accept any SQL expression via a `SQLRenderable` protocol, and extend `lit()` to work in SELECT, GROUP BY, and ORDER BY positions.

**Architecture:** New `storm/expression.py` defines a `typing.Protocol` class `SQLRenderable` with a single method `render_sql(params) -> str`. `ColumnProxy`, `Literal`, and `Predicate` each gain a `render_sql` method. The executor is refactored to call `render_sql` instead of directly accessing `ColumnProxy` internals.

**Tech Stack:** Python 3.12+, mypy strict, ruff, pytest with asyncio_mode=auto

---

### Task 1: Create the SQLRenderable protocol

**Files:**
- Create: `storm/expression.py`
- Test: `tests/test_expression.py`

- [ ] **Step 1: Write the test for the protocol module**

```python
# tests/test_expression.py
from __future__ import annotations

from typing import Any

from storm.expression import SQLRenderable


class TestSQLRenderableProtocol:
    def test_protocol_is_satisfiable(self):
        """A class with render_sql(params) -> str satisfies the protocol."""

        class FakeExpr:
            def render_sql(self, params: list[Any]) -> str:
                return "fake"

        expr: SQLRenderable = FakeExpr()
        assert expr.render_sql([]) == "fake"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `just test`
Expected: FAIL — `storm.expression` does not exist yet.

- [ ] **Step 3: Create the protocol module**

```python
# storm/expression.py
from __future__ import annotations

from typing import Any, Protocol


class SQLRenderable(Protocol):
    def render_sql(self, params: list[Any]) -> str: ...
```

- [ ] **Step 4: Run test to verify it passes**

Run: `just test`
Expected: PASS

- [ ] **Step 5: Run type checker**

Run: `just typecheck`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add storm/expression.py tests/test_expression.py
git commit -m "Add SQLRenderable protocol in storm/expression.py"
```

---

### Task 2: Add render_sql to ColumnProxy

**Files:**
- Modify: `storm/proxy.py`
- Test: `tests/test_expression.py`

- [ ] **Step 1: Write the test**

Add to `tests/test_expression.py`:

```python
from tests.conftest import AccountTable, LogTable, TaggedTable


class TestColumnProxyRenderSQL:
    def test_renders_qualified_name(self):
        params: list = []
        sql = AccountTable.name.render_sql(params)
        assert sql == "accounts.name"
        assert params == []

    def test_renders_with_table_override(self):
        params: list = []
        sql = LogTable.message.render_sql(params)
        assert sql == "log_entries.message"
        assert params == []

    def test_renders_column_rename(self):
        params: list = []
        sql = TaggedTable.tag.render_sql(params)
        assert sql == "tagged_accounts.tag_name"
        assert params == []
```

- [ ] **Step 2: Run to verify it fails**

Run: `just test`
Expected: FAIL — `ColumnProxy` has no `render_sql` method.

- [ ] **Step 3: Add render_sql to ColumnProxy**

In `storm/proxy.py`, add this method to the `ColumnProxy` class, after the `__ge__` method:

```python
    def render_sql(self, params: list[Any]) -> str:
        return f"{self._table._meta.table_name}.{self._field.column_name}"
```

Add `Any` to the existing `from __future__ import annotations` file — it already has that. Add `from typing import Any` to the imports.

- [ ] **Step 4: Run tests**

Run: `just test`
Expected: PASS

- [ ] **Step 5: Run type checker**

Run: `just typecheck`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add storm/proxy.py tests/test_expression.py
git commit -m "Add render_sql to ColumnProxy"
```

---

### Task 3: Add render_sql to Literal

**Files:**
- Modify: `storm/predicate.py`
- Test: `tests/test_expression.py`

- [ ] **Step 1: Write the test**

Add to `tests/test_expression.py`:

```python
import storm


class TestLiteralRenderSQL:
    def test_renders_raw_sql(self):
        params: list = []
        lit = storm.lit("COUNT(*)")
        sql = lit.render_sql(params)
        assert sql == "COUNT(*)"
        assert params == []

    def test_ignores_params(self):
        params = ["existing"]
        lit = storm.lit("NOW()")
        sql = lit.render_sql(params)
        assert sql == "NOW()"
        assert params == ["existing"]
```

- [ ] **Step 2: Run to verify it fails**

Run: `just test`
Expected: FAIL — `Literal` has no `render_sql` method.

- [ ] **Step 3: Add render_sql to Literal**

In `storm/predicate.py`, add a method to the `Literal` class:

```python
@dataclass(frozen=True)
class Literal:
    """Raw SQL fragment. No parameter substitution."""

    sql: str

    def render_sql(self, params: list[Any]) -> str:
        return self.sql
```

- [ ] **Step 4: Run tests**

Run: `just test`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add storm/predicate.py tests/test_expression.py
git commit -m "Add render_sql to Literal"
```

---

### Task 4: Rename Predicate.render to render_sql and rework internals

**Files:**
- Modify: `storm/predicate.py`
- Modify: `tests/test_predicate.py`
- Test: `tests/test_expression.py`

This is the most involved task. `Predicate.render` becomes `render_sql`, and the internals switch from `isinstance(x, ColumnProxy)` to `hasattr(x, "render_sql")`.

- [ ] **Step 1: Write new tests for Predicate.render_sql**

Add to `tests/test_expression.py`:

```python
from tests.conftest import AccountTable, LogTable


class TestPredicateRenderSQL:
    def test_simple_predicate(self):
        params: list = []
        pred = AccountTable.name == "Fred"
        sql = pred.render_sql(params)
        assert sql == "accounts.name = $1"
        assert params == ["Fred"]

    def test_column_to_column(self):
        params: list = []
        pred = AccountTable.id == LogTable.account_id
        sql = pred.render_sql(params)
        assert sql == "accounts.id = log_entries.account_id"
        assert params == []

    def test_compound_and(self):
        params: list = []
        pred = (AccountTable.name == "Fred") & (AccountTable.id > 1)
        sql = pred.render_sql(params)
        assert sql == "(accounts.name = $1) AND (accounts.id > $2)"
        assert params == ["Fred", 1]
```

- [ ] **Step 2: Run to verify the new tests fail**

Run: `pytest tests/test_expression.py::TestPredicateRenderSQL -v`
Expected: FAIL — `Predicate` has no `render_sql`.

- [ ] **Step 3: Rename render to render_sql and rework internals**

Replace the `Predicate` class in `storm/predicate.py` with:

```python
@dataclass
class Predicate:
    left: Any
    op: str
    right: Any

    def __and__(self, other: Predicate) -> Predicate:
        return Predicate(self, "AND", other)

    def __or__(self, other: Predicate) -> Predicate:
        return Predicate(self, "OR", other)

    def render_sql(self, params: list[Any]) -> str:
        if self.op in ("AND", "OR"):
            left_sql = f"({self.left.render_sql(params)})"
            right_sql = f"({self.right.render_sql(params)})"
        else:
            left_sql = self._render_operand(self.left, params)
            right_sql = self._render_operand(self.right, params)

        return f"{left_sql} {self.op} {right_sql}"

    @staticmethod
    def _render_operand(value: Any, params: list[Any]) -> str:
        if hasattr(value, "render_sql"):
            return value.render_sql(params)
        params.append(value)
        return f"${len(params)}"
```

Note: this removes the `from .proxy import ColumnProxy` import from `predicate.py` entirely — no more TYPE_CHECKING block needed.

- [ ] **Step 4: Run the new tests**

Run: `pytest tests/test_expression.py::TestPredicateRenderSQL -v`
Expected: PASS

- [ ] **Step 5: Update existing predicate tests to use render_sql**

In `tests/test_predicate.py`, replace every `.render(params)` call with `.render_sql(params)`. There are 9 occurrences across the file. Each line like:

```python
        sql = (AccountTable.name == "Fred").render(params)
```

becomes:

```python
        sql = (AccountTable.name == "Fred").render_sql(params)
```

Apply this to all 9 calls: lines 9, 14, 20, 28, 34, 42, 52, 58, 64 (the `.render(params)` calls in `test_equality_renders`, `test_inequality_renders`, `test_lt_gt`, `test_and_compound`, `test_or_compound`, `test_nested_compound`, `test_lt_renders`, `test_le_renders`, `test_ge_renders`).

Also update `test_column_to_column_renders_as_columns` (line 71) and `test_params_accumulate_across_predicates` (lines 78-79).

- [ ] **Step 6: Run all tests**

Run: `just test`
Expected: PASS — all existing and new tests pass.

- [ ] **Step 7: Run type checker and linter**

Run: `just check`
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add storm/predicate.py tests/test_predicate.py tests/test_expression.py
git commit -m "Rename Predicate.render to render_sql, use hasattr dispatch"
```

---

### Task 5: Refactor executor to use render_sql

**Files:**
- Modify: `storm/executor.py`

This task changes the executor to call `render_sql(params)` instead of directly accessing `ColumnProxy` internals. All existing tests must continue to pass — no new tests needed since the SQL output is identical.

- [ ] **Step 1: Refactor _render_where**

In `storm/executor.py`, replace the `_render_where` method:

```python
    def _render_where(self, predicates: list[Any], params: list[Any]) -> str:
        """Render a WHERE clause from a list of renderable predicates."""
        parts: list[str] = []
        for p in predicates:
            parts.append(f"({p.render_sql(params)})")
        return " AND ".join(parts)
```

Remove the `Literal` import from the top of the file (it's no longer needed by `_render_where`). Keep the `_All` import — it's still used by `_check_predicates`.

- [ ] **Step 2: Run all tests**

Run: `just test`
Expected: PASS

- [ ] **Step 3: Refactor _render_select column list**

In `storm/executor.py`, in `_render_select`, replace the column list rendering (the `if b._columns:` branch):

```python
        if b._columns:
            cols = ", ".join(c.render_sql(params) for c in b._columns)
        else:
```

The `else` branch (for `table.*` and join wildcards) stays unchanged — it doesn't involve expressions.

- [ ] **Step 4: Run all tests**

Run: `just test`
Expected: PASS

- [ ] **Step 5: Refactor GROUP BY rendering**

In `_render_select`, replace the GROUP BY block:

```python
        if b._group:
            group_cols = ", ".join(c.render_sql(params) for c in b._group)
            sql += f" GROUP BY {group_cols}"
```

- [ ] **Step 6: Run all tests**

Run: `just test`
Expected: PASS

- [ ] **Step 7: Refactor ORDER BY rendering**

In `_render_select`, replace the ORDER BY block. This one has the special handling for non-ColumnProxy expressions (literals control their own direction):

```python
        if b._order:
            from .proxy import ColumnProxy

            order_parts: list[str] = []
            for c, d in b._order:
                rendered = c.render_sql(params)
                if isinstance(c, ColumnProxy):
                    rendered += f" {d}"
                order_parts.append(rendered)
            sql += f" ORDER BY {', '.join(order_parts)}"
```

- [ ] **Step 8: Run all tests**

Run: `just test`
Expected: PASS

- [ ] **Step 9: Clean up imports**

In `storm/executor.py`, remove the `Literal` import from the top-level imports since it's no longer used. The import line:

```python
from .predicate import Literal, _All
```

becomes:

```python
from .predicate import _All
```

- [ ] **Step 10: Run full check**

Run: `just check`
Expected: PASS — tests, linter, formatter, type checker all green.

- [ ] **Step 11: Commit**

```bash
git add storm/executor.py
git commit -m "Refactor executor to use render_sql instead of ColumnProxy internals"
```

---

### Task 6: Add tests for lit() in new positions

**Files:**
- Test: `tests/test_builders.py`

Now that the executor uses `render_sql`, `lit()` should work in SELECT, ORDER BY, and GROUP BY. Add tests that prove it.

- [ ] **Step 1: Add lit() SELECT test**

Add to `tests/test_builders.py`, in the `TestLiteralSQL` class:

```python
    async def test_lit_in_select_columns(self):
        db = FakeDB(rows=[("Fred", 1)])
        await storm.SELECT(db, AccountTable.name, storm.lit("1 AS one")).FROM(
            AccountTable
        )
        assert db.last_sql == "SELECT accounts.name, 1 AS one FROM accounts"
        assert db.last_params == []
```

- [ ] **Step 2: Run to verify it passes**

Run: `pytest tests/test_builders.py::TestLiteralSQL::test_lit_in_select_columns -v`
Expected: PASS — the executor already handles this after the Task 5 refactor.

- [ ] **Step 3: Add lit()-only SELECT test**

Add to `TestLiteralSQL`:

```python
    async def test_lit_only_select(self):
        db = FakeDB(rows=[(5,)])
        await storm.SELECT(db, storm.lit("COUNT(*)")).FROM(AccountTable)
        assert db.last_sql == "SELECT COUNT(*) FROM accounts"
        assert db.last_params == []
```

- [ ] **Step 4: Run to verify it passes**

Run: `pytest tests/test_builders.py::TestLiteralSQL::test_lit_only_select -v`
Expected: PASS

- [ ] **Step 5: Add lit() ORDER BY test**

Add to `TestLiteralSQL`:

```python
    async def test_lit_in_order_by(self):
        db = FakeDB(rows=[])
        await storm.SELECT(db).FROM(AccountTable).ORDER_BY(
            storm.lit("created_at DESC")
        )
        assert db.last_sql == (
            "SELECT accounts.* FROM accounts ORDER BY created_at DESC"
        )
        assert db.last_params == []
```

- [ ] **Step 6: Run to verify it passes**

Run: `pytest tests/test_builders.py::TestLiteralSQL::test_lit_in_order_by -v`
Expected: PASS — the ORDER BY refactor in Task 5 skips direction for non-ColumnProxy.

- [ ] **Step 7: Add lit() GROUP BY test**

Add to `TestLiteralSQL`:

```python
    async def test_lit_in_group_by(self):
        db = FakeDB(rows=[("Fred", 3)])
        await (
            storm.SELECT(db, AccountTable.name, storm.lit("COUNT(*) AS cnt"))
            .FROM(AccountTable)
            .GROUP_BY(storm.lit("name"))
        )
        assert "GROUP BY name" in db.last_sql
        assert db.last_params == []
```

- [ ] **Step 8: Run to verify it passes**

Run: `pytest tests/test_builders.py::TestLiteralSQL::test_lit_in_group_by -v`
Expected: PASS

- [ ] **Step 9: Run full test suite**

Run: `just check`
Expected: PASS — all tests, lint, types.

- [ ] **Step 10: Commit**

```bash
git add tests/test_builders.py
git commit -m "Add tests for lit() in SELECT, ORDER BY, and GROUP BY positions"
```

---

### Task 7: Update docstrings, type hints, and CLAUDE.md

**Files:**
- Modify: `storm/__init__.py`
- Modify: `storm/predicate.py`
- Modify: `storm/builders.py`
- Modify: `CLAUDE.md`

- [ ] **Step 1: Update builder type hints**

In `storm/builders.py`, add the import at the top (TYPE_CHECKING-only to avoid circular imports):

```python
from __future__ import annotations

from collections.abc import Generator
from typing import TYPE_CHECKING, Any

from .predicate import Literal, Predicate, _All
from .proxy import TableProxy

if TYPE_CHECKING:
    from .expression import SQLRenderable
```

Then update the `SelectBuilder` method signatures. In `__init__`:

```python
    def __init__(self, db: Any, *columns: SQLRenderable) -> None:
```

In `ORDER_BY`:

```python
    def ORDER_BY(self, *columns: SQLRenderable, DESC: bool = False) -> SelectBuilder:
```

In `GROUP_BY`:

```python
    def GROUP_BY(self, *columns: SQLRenderable) -> SelectBuilder:
```

The WHERE signatures stay as `Predicate | Literal | _All` since those are the actual accepted types for predicates.

- [ ] **Step 2: Run type checker**

Run: `just check`
Expected: PASS

- [ ] **Step 3: Update lit() docstring**

In `storm/__init__.py`, change the `lit` function docstring:

```python
def lit(sql: str) -> Literal:
    """Create a raw SQL literal for use in any expression position."""
    return Literal(sql=sql)
```

- [ ] **Step 2: Update Literal class docstring**

In `storm/predicate.py`, update the `Literal` docstring:

```python
@dataclass(frozen=True)
class Literal:
    """Raw SQL fragment for use in any expression position. No parameter substitution."""

    sql: str

    def render_sql(self, params: list[Any]) -> str:
        return self.sql
```

- [ ] **Step 3: Run full check**

Run: `just check`
Expected: PASS

- [ ] **Step 4: Update CLAUDE.md**

In `CLAUDE.md`, update the architecture section to mention `expression.py`:

```
storm/
  expression.py — SQLRenderable protocol: the contract for any SQL expression
  __init__.py    — Public API: Table(), SELECT/INSERT/UPDATE(), get(), save(), transaction
  ...
```

Update the `storm.lit()` bullet point in Key patterns:

```
- **`storm.lit(sql)`** — raw SQL literal for any expression position (SELECT, WHERE, GROUP BY, ORDER BY). No parameter substitution.
```

Add a new bullet to Key patterns for the expression protocol:

```
- **Expression protocol.** Any type with a `render_sql(self, params: list[Any]) -> str` method can appear where a `ColumnProxy` goes (SELECT columns, WHERE, GROUP BY, ORDER BY). `ColumnProxy`, `Literal`, and `Predicate` all satisfy `SQLRenderable` (defined in `expression.py`).
```

- [ ] **Step 5: Run full check**

Run: `just check`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add storm/__init__.py storm/predicate.py storm/builders.py CLAUDE.md
git commit -m "Update docstrings, type hints, and CLAUDE.md for expression protocol"
```
