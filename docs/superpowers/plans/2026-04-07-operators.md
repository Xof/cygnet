# Operators Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `storm.op()`, `storm.ops()`, `storm.is_null()`, `storm.is_not_null()` for SQL operators that don't map to Python comparison operators.

**Architecture:** Infix operators reuse `Predicate` directly. Two new frozen dataclasses (`PrefixOp`, `SuffixOp`) in `storm/expression.py` handle prefix/suffix operators, each with `render_sql` and `__and__`/`__or__` for compound support. Factory functions `op()`, `ops()`, `is_null()`, `is_not_null()` are the public API, re-exported through `storm/__init__.py`.

**Tech Stack:** Python 3.12+, mypy strict, ruff, pytest with asyncio_mode=auto

---

### Task 1: Add PrefixOp and SuffixOp classes

**Files:**
- Modify: `storm/expression.py`
- Test: `tests/test_expression.py`

- [ ] **Step 1: Write tests for PrefixOp**

Add to `tests/test_expression.py`:

```python
from storm.expression import PrefixOp, SuffixOp


class TestPrefixOp:
    def test_renders_prefix(self):
        params: list = []
        expr = AccountTable.name == "Fred"
        prefix = PrefixOp(op="NOT", operand=expr)
        sql = prefix.render_sql(params)
        assert sql == "NOT (accounts.name = $1)"
        assert params == ["Fred"]

    def test_and_compound(self):
        params: list = []
        prefix = PrefixOp(op="NOT", operand=AccountTable.name == "Fred")
        compound = prefix & (AccountTable.id > 1)
        sql = compound.render_sql(params)
        assert sql == "(NOT (accounts.name = $1)) AND (accounts.id > $2)"
        assert params == ["Fred", 1]

    def test_or_compound(self):
        params: list = []
        prefix = PrefixOp(op="NOT", operand=AccountTable.name == "Fred")
        compound = prefix | (AccountTable.id > 1)
        sql = compound.render_sql(params)
        assert sql == "(NOT (accounts.name = $1)) OR (accounts.id > $2)"
        assert params == ["Fred", 1]
```

- [ ] **Step 2: Run to verify they fail**

Run: `pytest tests/test_expression.py::TestPrefixOp -v`
Expected: FAIL — `PrefixOp` does not exist.

- [ ] **Step 3: Write tests for SuffixOp**

Add to `tests/test_expression.py`:

```python
class TestSuffixOp:
    def test_renders_suffix(self):
        params: list = []
        suffix = SuffixOp(operand=AccountTable.email, op="IS NULL")
        sql = suffix.render_sql(params)
        assert sql == "accounts.email IS NULL"
        assert params == []

    def test_and_compound(self):
        params: list = []
        suffix = SuffixOp(operand=AccountTable.email, op="IS NULL")
        compound = suffix & (AccountTable.name == "Fred")
        sql = compound.render_sql(params)
        assert sql == "(accounts.email IS NULL) AND (accounts.name = $1)"
        assert params == ["Fred"]

    def test_or_compound(self):
        params: list = []
        suffix = SuffixOp(operand=AccountTable.email, op="IS NOT NULL")
        compound = suffix | (AccountTable.name == "Fred")
        sql = compound.render_sql(params)
        assert sql == "(accounts.email IS NOT NULL) OR (accounts.name = $1)"
        assert params == ["Fred"]
```

- [ ] **Step 4: Implement PrefixOp and SuffixOp**

Replace the entire contents of `storm/expression.py` with:

```python
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


class SQLRenderable(Protocol):
    def render_sql(self, params: list[Any]) -> str: ...


@dataclass(frozen=True)
class PrefixOp:
    """Prefix operator: renders as 'OP (expr)', e.g., NOT (accounts.active = $1)."""

    op: str
    operand: Any

    def render_sql(self, params: list[Any]) -> str:
        return f"{self.op} ({self.operand.render_sql(params)})"

    def __and__(self, other: Any) -> Predicate:
        return Predicate(self, "AND", other)

    def __or__(self, other: Any) -> Predicate:
        return Predicate(self, "OR", other)


@dataclass(frozen=True)
class SuffixOp:
    """Suffix operator: renders as 'expr OP', e.g., accounts.email IS NULL."""

    operand: Any
    op: str

    def render_sql(self, params: list[Any]) -> str:
        return f"{self.operand.render_sql(params)} {self.op}"

    def __and__(self, other: Any) -> Predicate:
        return Predicate(self, "AND", other)

    def __or__(self, other: Any) -> Predicate:
        return Predicate(self, "OR", other)
```

Note: `Predicate` is used in the return types of `__and__`/`__or__`. Because of `from __future__ import annotations`, the forward reference resolves at type-check time. At runtime, we need the import. Add at the bottom of the file (after the class definitions to avoid circular import issues at module level):

```python
from .predicate import Predicate  # noqa: E402
```

Actually, since `expression.py` importing from `predicate.py` is safe (no circular dependency), place the import at the top with the others:

```python
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from .predicate import Predicate


class SQLRenderable(Protocol):
    ...
```

- [ ] **Step 5: Run tests**

Run: `pytest tests/ --ignore=tests/integration -v`
Expected: PASS — all new and existing tests.

- [ ] **Step 6: Run type checker**

Run: `mypy storm`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add storm/expression.py tests/test_expression.py
git commit -m "Add PrefixOp and SuffixOp expression classes"
```

---

### Task 2: Add op(), ops(), is_null(), is_not_null() factory functions

**Files:**
- Modify: `storm/expression.py`
- Test: `tests/test_expression.py`

- [ ] **Step 1: Write tests for op() infix**

Add to `tests/test_expression.py`:

```python
import storm


class TestOpFunction:
    def test_infix_3arg(self):
        params: list = []
        pred = storm.op(AccountTable.name, "ILIKE", "%fred%")
        sql = pred.render_sql(params)
        assert sql == "accounts.name ILIKE $1"
        assert params == ["%fred%"]

    def test_prefix_2arg(self):
        params: list = []
        expr = storm.op("NOT", AccountTable.name == "Fred")
        sql = expr.render_sql(params)
        assert sql == "NOT (accounts.name = $1)"
        assert params == ["Fred"]

    def test_precreated_1arg(self):
        ILIKE = storm.op("ILIKE")
        params: list = []
        pred = ILIKE(AccountTable.name, "%fred%")
        sql = pred.render_sql(params)
        assert sql == "accounts.name ILIKE $1"
        assert params == ["%fred%"]

    def test_zero_args_raises(self):
        with pytest.raises(TypeError, match="requires 1, 2, or 3 arguments"):
            storm.op()

    def test_four_args_raises(self):
        with pytest.raises(TypeError, match="requires 1, 2, or 3 arguments"):
            storm.op("a", "b", "c", "d")
```

- [ ] **Step 2: Write tests for ops(), is_null(), is_not_null()**

Add to `tests/test_expression.py`:

```python
class TestOpsFunction:
    def test_suffix(self):
        params: list = []
        expr = storm.ops(AccountTable.email, "IS NULL")
        sql = expr.render_sql(params)
        assert sql == "accounts.email IS NULL"
        assert params == []


class TestIsNullIsNotNull:
    def test_is_null(self):
        params: list = []
        expr = storm.is_null(AccountTable.email)
        sql = expr.render_sql(params)
        assert sql == "accounts.email IS NULL"
        assert params == []

    def test_is_not_null(self):
        params: list = []
        expr = storm.is_not_null(AccountTable.email)
        sql = expr.render_sql(params)
        assert sql == "accounts.email IS NOT NULL"
        assert params == []
```

- [ ] **Step 3: Run to verify they fail**

Run: `pytest tests/test_expression.py::TestOpFunction -v`
Expected: FAIL — `storm.op` does not exist.

- [ ] **Step 4: Add factory functions to expression.py**

Add at the bottom of `storm/expression.py`:

```python
def op(*args: Any) -> Predicate | PrefixOp | Any:
    """Create an operator expression.

    - 3 args: op(left, 'ILIKE', right) → infix Predicate
    - 2 args: op('NOT', expr) → PrefixOp
    - 1 arg:  op('ILIKE') → reusable callable returning Predicate
    """
    if len(args) == 3:
        return Predicate(args[0], args[1], args[2])
    if len(args) == 2:
        return PrefixOp(op=args[0], operand=args[1])
    if len(args) == 1:
        operator = args[0]

        def _precreated(left: Any, right: Any) -> Predicate:
            return Predicate(left, operator, right)

        return _precreated
    raise TypeError(f"storm.op() requires 1, 2, or 3 arguments, got {len(args)}")


def ops(operand: Any, operator: str) -> SuffixOp:
    """Create a suffix operator: ops(col, 'IS NULL') → col IS NULL."""
    return SuffixOp(operand=operand, op=operator)


def is_null(operand: Any) -> SuffixOp:
    """Convenience: is_null(col) → col IS NULL."""
    return SuffixOp(operand=operand, op="IS NULL")


def is_not_null(operand: Any) -> SuffixOp:
    """Convenience: is_not_null(col) → col IS NOT NULL."""
    return SuffixOp(operand=operand, op="IS NOT NULL")
```

- [ ] **Step 5: Add exports to storm/__init__.py**

In `storm/__init__.py`, add the import:

```python
from .expression import is_not_null, is_null, op, ops
```

And add to `__all__`, after `"lit"`:

```python
    "op",
    "ops",
    "is_null",
    "is_not_null",
```

- [ ] **Step 6: Add pytest import to test file**

Make sure `tests/test_expression.py` imports `pytest` at the top (needed for `pytest.raises`):

```python
import pytest
```

- [ ] **Step 7: Run tests**

Run: `pytest tests/ --ignore=tests/integration -v`
Expected: PASS

- [ ] **Step 8: Run type checker and linter**

Run: `mypy storm && ruff check storm tests && ruff format --check storm tests`
Expected: PASS

- [ ] **Step 9: Commit**

```bash
git add storm/expression.py storm/__init__.py tests/test_expression.py
git commit -m "Add op(), ops(), is_null(), is_not_null() factory functions"
```

---

### Task 3: Add WHERE integration tests

**Files:**
- Test: `tests/test_builders.py`

- [ ] **Step 1: Write WHERE integration tests**

Add a new test class to `tests/test_builders.py`:

```python
class TestOperatorSQL:
    async def test_op_infix_in_where(self):
        db = FakeDB(rows=[])
        await (
            storm.SELECT(db)
            .FROM(AccountTable)
            .WHERE(storm.op(AccountTable.name, "ILIKE", "%fred%"))
        )
        assert db.last_sql == (
            "SELECT accounts.* FROM accounts "
            "WHERE (accounts.name ILIKE $1)"
        )
        assert db.last_params == ["%fred%"]

    async def test_is_null_in_where(self):
        db = FakeDB(rows=[])
        await (
            storm.SELECT(db)
            .FROM(AccountTable)
            .WHERE(storm.is_null(AccountTable.email))
        )
        assert db.last_sql == (
            "SELECT accounts.* FROM accounts "
            "WHERE (accounts.email IS NULL)"
        )
        assert db.last_params == []

    async def test_compound_op_in_where(self):
        db = FakeDB(rows=[])
        await (
            storm.SELECT(db)
            .FROM(AccountTable)
            .WHERE(
                storm.is_null(AccountTable.email)
                & (AccountTable.name == "Fred")
            )
        )
        assert db.last_sql == (
            "SELECT accounts.* FROM accounts "
            "WHERE ((accounts.email IS NULL) "
            "AND (accounts.name = $1))"
        )
        assert db.last_params == ["Fred"]

    async def test_prefix_op_in_where(self):
        db = FakeDB(rows=[])
        await (
            storm.SELECT(db)
            .FROM(AccountTable)
            .WHERE(storm.op("NOT", AccountTable.name == "Fred"))
        )
        assert db.last_sql == (
            "SELECT accounts.* FROM accounts "
            "WHERE (NOT (accounts.name = $1))"
        )
        assert db.last_params == ["Fred"]
```

- [ ] **Step 2: Run to verify they pass**

Run: `pytest tests/test_builders.py::TestOperatorSQL -v`
Expected: PASS — the operator classes and factory functions already work;
this just proves they integrate with the builder/executor pipeline.

- [ ] **Step 3: Run full check**

Run: `mypy storm && ruff check storm tests && ruff format --check storm tests && pytest tests/ --ignore=tests/integration`
Expected: all pass

- [ ] **Step 4: Commit**

```bash
git add tests/test_builders.py
git commit -m "Add WHERE integration tests for operator expressions"
```

---

### Task 4: Update CLAUDE.md

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Add operator documentation to Key patterns**

In `CLAUDE.md`, add these bullets to the Key patterns section, after the
expression protocol bullet:

```
- **`storm.op()`** — custom operators. 3-arg infix: `op(T.col, 'ILIKE', val)`. 2-arg prefix: `op('NOT', expr)`. 1-arg pre-created: `ILIKE = op('ILIKE')` returns a reusable callable.
- **`storm.ops()`** — suffix operators: `ops(T.col, 'IS NULL')`.
- **`storm.is_null(col)` / `storm.is_not_null(col)`** — convenience for IS NULL / IS NOT NULL.
```

- [ ] **Step 2: Add to __all__ list in the export documentation if present**

No separate export docs — `__all__` in `__init__.py` was already updated in Task 2.

- [ ] **Step 3: Run full check**

Run: `mypy storm && ruff check storm tests && ruff format --check storm tests && pytest tests/ --ignore=tests/integration`
Expected: all pass

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md
git commit -m "Document operator functions in CLAUDE.md"
```
