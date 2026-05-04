# Foreign Key Follow Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add FK annotation, metadata introspection, `storm.follow()` function, and `FOLLOW()`/`LEFT_FOLLOW()` builder methods so users can declare and traverse foreign key relationships with minimal ceremony.

**Architecture:** `_ForeignKey` annotation marker in `annotations.py` → introspected by `meta.py` into `FieldMeta.foreign_key` → consumed by `follow()` in `__init__.py` and `FOLLOW()`/`LEFT_FOLLOW()` in `builders.py`. No changes to SQL rendering, row mapping, predicates, or the expression protocol.

**Tech Stack:** Python 3.12+, dataclasses, `Annotated` type hints, pytest (async auto mode)

---

### Task 1: Add `_ForeignKey` annotation and `ForeignKey()` factory

**Files:**
- Modify: `storm/annotations.py:1-58`
- Test: `tests/test_meta.py`

- [ ] **Step 1: Write failing tests for FK annotation recognition**

Add to `tests/test_meta.py`:

```python
class TestForeignKey:
    def test_fk_recognised(self):
        @dataclasses.dataclass
        class Parent:
            id: Annotated[int, DBKey]
            name: str

        @dataclasses.dataclass
        class Child:
            id: Annotated[int, DBKey]
            parent_id: Annotated[int, storm.ForeignKey(Parent)]

        meta = TableMeta(Child)
        fk_field = next(f for f in meta.fields if f.attr_name == "parent_id")
        assert fk_field.foreign_key is not None
        assert fk_field.foreign_key.target is Parent

    def test_non_fk_field_has_none(self):
        meta = TableMeta(Account)
        name_field = next(f for f in meta.fields if f.attr_name == "name")
        assert name_field.foreign_key is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `just test`
Expected: FAIL — `storm.ForeignKey` does not exist, `FieldMeta` has no `foreign_key` attribute.

- [ ] **Step 3: Add `_ForeignKey` dataclass and `ForeignKey()` factory to `annotations.py`**

Add at end of `storm/annotations.py`:

```python
@dataclass(frozen=True)
class _ForeignKey:
    # The target dataclass whose primary key this field references.
    # Resolved lazily by meta.py via get_type_hints(include_extras=True),
    # so forward references and circular imports are not a problem.
    target: type


def ForeignKey(target: type) -> _ForeignKey:  # noqa: N802
    # Factory function matching the Column() / DBKey / AppKey pattern.
    # Users write ForeignKey(Customer), not _ForeignKey(target=Customer).
    return _ForeignKey(target=target)
```

- [ ] **Step 4: Export `ForeignKey` from `storm/__init__.py`**

Add `ForeignKey` to the import from `.annotations` and to `__all__`:

In the import line, change:
```python
from .annotations import AppKey, Column, DBKey, table
```
to:
```python
from .annotations import AppKey, Column, DBKey, ForeignKey, table
```

In `__all__`, add `"ForeignKey"` after `"Column"`:
```python
__all__ = [
    # Annotations
    "DBKey",
    "AppKey",
    "Column",
    "ForeignKey",
    "table",
    ...
]
```

- [ ] **Step 5: Add `foreign_key` field to `FieldMeta` in `meta.py`**

Change `FieldMeta` in `storm/meta.py`:

```python
@dataclasses.dataclass
class FieldMeta:
    """One field's worth of introspection results.

    attr_name and column_name may differ when Column("...") overrides the
    default.  primary_key is None for non-PK fields.  foreign_key is None
    for non-FK fields.
    """

    attr_name: str
    column_name: str
    python_type: type
    primary_key: _PrimaryKey | None
    foreign_key: _ForeignKey | None = None
```

- [ ] **Step 6: Scan for `_ForeignKey` in `_introspect()`**

In `storm/meta.py`, add `_ForeignKey` to the import:

```python
from .annotations import DBKey, _Column, _ForeignKey, _PrimaryKey
```

In `_introspect()`, add an `fk_meta` variable and scan for it alongside PK/Column. Change the annotation scanning loop and FieldMeta construction:

```python
    def _introspect(self) -> None:
        if not dataclasses.is_dataclass(self.cls):
            raise TypeError(
                f"{self.cls.__name__} is not a dataclass — "
                f"STORM requires dataclasses as model objects"
            )

        hints = get_type_hints(self.cls, include_extras=True)
        for attr, hint in hints.items():
            pk_meta: _PrimaryKey | None = None
            fk_meta: _ForeignKey | None = None
            col_name = attr
            py_type = hint

            if get_origin(hint) is Annotated:
                args = get_args(hint)
                py_type = args[0]
                for a in args[1:]:
                    if isinstance(a, _PrimaryKey):
                        pk_meta = a
                    elif isinstance(a, _ForeignKey):
                        fk_meta = a
                    elif isinstance(a, _Column) and a.name:
                        col_name = a.name

            if pk_meta is not None:
                if self.pk is not None:
                    raise TypeError(
                        f"{self.cls.__name__} has more than one primary key annotation"
                    )
                if (
                    pk_meta == DBKey
                    and getattr(self.cls, "__dataclass_params__", None)
                    and self.cls.__dataclass_params__.frozen
                ):
                    raise TypeError(
                        f"{self.cls.__name__}: DBKey fields are incompatible "
                        f"with frozen=True — STORM cannot populate the key "
                        f"after INSERT. Use AppKey or remove frozen=True."
                    )

            fm = FieldMeta(attr, col_name, py_type, pk_meta, fk_meta)
            self.fields.append(fm)
            if pk_meta is not None:
                self.pk = fm
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `just test`
Expected: All tests pass, including the two new FK recognition tests.

- [ ] **Step 8: Commit**

```bash
git add storm/annotations.py storm/meta.py storm/__init__.py tests/test_meta.py
git commit -m "feat: add ForeignKey annotation and FieldMeta.foreign_key introspection"
```

---

### Task 2: Add FK validation in `_introspect()`

**Files:**
- Modify: `storm/meta.py:68-119`
- Test: `tests/test_meta.py`

- [ ] **Step 1: Write failing tests for FK validation**

Add to `TestForeignKey` class in `tests/test_meta.py`:

```python
    def test_fk_target_not_dataclass_raises(self):
        class NotADataclass:
            pass

        with pytest.raises(TypeError, match="not a dataclass"):

            @dataclasses.dataclass
            class BadChild:
                id: Annotated[int, DBKey]
                parent_id: Annotated[int, storm.ForeignKey(NotADataclass)]

            TableMeta(BadChild)

    def test_fk_target_no_pk_raises(self):
        @dataclasses.dataclass
        class NoPK:
            name: str

        with pytest.raises(TypeError, match="no primary key"):

            @dataclasses.dataclass
            class BadChild:
                id: Annotated[int, DBKey]
                parent_id: Annotated[int, storm.ForeignKey(NoPK)]

            TableMeta(BadChild)

    def test_fk_and_pk_on_same_field_raises(self):
        @dataclasses.dataclass
        class Parent:
            id: Annotated[int, DBKey]

        with pytest.raises(TypeError, match="cannot be both"):

            @dataclasses.dataclass
            class BadChild:
                id: Annotated[int, DBKey, storm.ForeignKey(Parent)]

            TableMeta(BadChild)

    def test_fk_type_mismatch_raises(self):
        @dataclasses.dataclass
        class Parent:
            id: Annotated[int, DBKey]

        with pytest.raises(TypeError, match="type mismatch"):

            @dataclasses.dataclass
            class BadChild:
                id: Annotated[int, DBKey]
                parent_id: Annotated[str, storm.ForeignKey(Parent)]

            TableMeta(BadChild)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `just test`
Expected: FAIL — no validation logic exists yet.

- [ ] **Step 3: Add FK validation after the existing PK validation block in `_introspect()`**

In `storm/meta.py`, add this block after the PK validation and before the `fm = FieldMeta(...)` line:

```python
            if fk_meta is not None:
                if pk_meta is not None:
                    raise TypeError(
                        f"{self.cls.__name__}.{attr}: a field cannot be both "
                        f"a primary key and a foreign key"
                    )
                # Introspect the target to validate it's a valid FK target.
                # This triggers the target's own introspection if not cached.
                target_meta = TableMeta(fk_meta.target)
                if target_meta.pk is None:
                    raise TypeError(
                        f"{self.cls.__name__}.{attr}: foreign key target "
                        f"{fk_meta.target.__name__} has no primary key"
                    )
                if py_type != target_meta.pk.python_type:
                    raise TypeError(
                        f"{self.cls.__name__}.{attr}: foreign key type mismatch — "
                        f"{py_type.__name__} != {target_meta.pk.python_type.__name__}"
                    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `just test`
Expected: All tests pass, including the four new validation tests.

- [ ] **Step 5: Commit**

```bash
git add storm/meta.py tests/test_meta.py
git commit -m "feat: validate FK annotations — target must be dataclass with PK, types must match"
```

---

### Task 3: Add `foreign_keys` property to `TableMeta`

**Files:**
- Modify: `storm/meta.py`
- Test: `tests/test_meta.py`

- [ ] **Step 1: Write failing tests**

Add to `TestForeignKey` class in `tests/test_meta.py`:

```python
    def test_foreign_keys_property(self):
        @dataclasses.dataclass
        class Parent:
            id: Annotated[int, DBKey]
            name: str

        @dataclasses.dataclass
        class Child:
            id: Annotated[int, DBKey]
            parent_id: Annotated[int, storm.ForeignKey(Parent)]
            name: str

        meta = TableMeta(Child)
        fks = meta.foreign_keys
        assert len(fks) == 1
        assert fks[0].attr_name == "parent_id"

    def test_foreign_keys_empty_when_none(self):
        meta = TableMeta(Account)
        assert meta.foreign_keys == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `just test`
Expected: FAIL — `TableMeta` has no `foreign_keys` property.

- [ ] **Step 3: Add `foreign_keys` property to `TableMeta`**

Add after the `_introspect` method in `storm/meta.py`:

```python
    @property
    def foreign_keys(self) -> list[FieldMeta]:
        """Fields that are foreign keys."""
        return [f for f in self.fields if f.foreign_key is not None]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `just test`
Expected: All tests pass.

- [ ] **Step 5: Commit**

```bash
git add storm/meta.py tests/test_meta.py
git commit -m "feat: add TableMeta.foreign_keys convenience property"
```

---

### Task 4: Add `storm.follow()` standalone function

**Files:**
- Modify: `storm/__init__.py:118-134`
- Test: `tests/test_builders.py`

- [ ] **Step 1: Write failing tests for `follow()`**

Add to `tests/test_builders.py`. First, add new model fixtures at the top of the file (after existing imports):

```python
from storm.annotations import DBKey


@dataclasses.dataclass
class Customer:
    id: Annotated[int, DBKey]
    name: str


@dataclasses.dataclass
class Order:
    id: Annotated[int, DBKey]
    customer_id: Annotated[int, storm.ForeignKey(Customer)]
    amount: float


CustomerTable = storm.Table(Customer)
OrderTable = storm.Table(Order)
```

Then add the test class:

```python
class TestFollowSQL:
    async def test_follow_generates_get(self):
        """follow() should query the target table by PK."""
        db = FakeDB(rows=[(10, "Alice")])
        order = Order(id=1, customer_id=10, amount=99.99)
        result = await storm.follow(db, order, OrderTable.customer_id)
        assert isinstance(result, Customer)
        assert result.name == "Alice"
        assert db.last_params == [10]
        assert "customers" in db.last_sql

    async def test_follow_none_fk_returns_none(self):
        """follow() with None FK value returns None without querying."""
        db = FakeDB()
        order = Order(id=1, customer_id=None, amount=99.99)
        result = await storm.follow(db, order, OrderTable.customer_id)
        assert result is None
        assert len(db.calls) == 0

    async def test_follow_not_found_returns_none(self):
        """follow() returns None when no matching row exists."""
        db = FakeDB(rows=[])
        order = Order(id=1, customer_id=999, amount=99.99)
        result = await storm.follow(db, order, OrderTable.customer_id)
        assert result is None

    async def test_follow_non_fk_column_raises(self):
        """follow() on a non-FK column raises ValueError."""
        db = FakeDB()
        order = Order(id=1, customer_id=10, amount=99.99)
        with pytest.raises(ValueError, match="not a foreign key"):
            await storm.follow(db, order, OrderTable.amount)

    async def test_follow_wrong_object_type_raises(self):
        """follow() with wrong object type raises TypeError."""
        db = FakeDB()
        acc = Account(id=1, name="Fred", email="fred@example.com")
        with pytest.raises(TypeError, match="Expected Order"):
            await storm.follow(db, acc, OrderTable.customer_id)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `just test`
Expected: FAIL — `storm.follow` does not exist.

- [ ] **Step 3: Implement `follow()` in `storm/__init__.py`**

Add after the `get()` function:

```python
async def follow(db: Any, obj: Any, fk_column: Any) -> Any:
    """Load the object that a foreign key points to.

    Returns None if the FK value is None or no matching row exists.
    Raises ValueError if fk_column is not a foreign key.
    Raises TypeError if obj is not an instance of the FK column's table.
    """
    from .proxy import ColumnProxy, TableProxy

    if not isinstance(fk_column, ColumnProxy):
        raise ValueError(f"{fk_column!r} is not a column proxy")

    field = fk_column._field
    source_meta = fk_column._table._meta

    if not isinstance(obj, source_meta.cls):
        raise TypeError(
            f"Expected {source_meta.cls.__name__}, "
            f"got {type(obj).__name__}"
        )

    if field.foreign_key is None:
        raise ValueError(
            f"{source_meta.cls.__name__}.{field.attr_name} is not a foreign key"
        )

    fk_value = getattr(obj, field.attr_name)
    if fk_value is None:
        return None

    target_proxy = TableProxy(field.foreign_key.target)
    target_pk = target_proxy._meta.pk
    return await get(db, target_proxy, **{target_pk.attr_name: fk_value})
```

Add `"follow"` to `__all__`:

```python
    # Convenience
    "create",
    "follow",
    "get",
    "save",
    "transaction",
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `just test`
Expected: All tests pass.

- [ ] **Step 5: Commit**

```bash
git add storm/__init__.py tests/test_builders.py
git commit -m "feat: add storm.follow() — load related object via FK column"
```

---

### Task 5: Add `FOLLOW()` and `LEFT_FOLLOW()` builder methods

**Files:**
- Modify: `storm/builders.py:44-109`
- Test: `tests/test_builders.py`

- [ ] **Step 1: Write failing tests for `FOLLOW()` and `LEFT_FOLLOW()`**

Add to `tests/test_builders.py`:

```python
class TestFollowBuilderSQL:
    async def test_follow_generates_inner_join(self):
        """FOLLOW() should produce an INNER JOIN with the correct ON condition."""
        db = FakeDB(rows=[])
        await storm.SELECT(db).FROM(OrderTable).FOLLOW(OrderTable.customer_id)
        assert "INNER JOIN customers ON" in db.last_sql
        assert "orders.customer_id = customers.id" in db.last_sql

    async def test_left_follow_generates_left_join(self):
        """LEFT_FOLLOW() should produce a LEFT JOIN."""
        db = FakeDB(rows=[])
        await storm.SELECT(db).FROM(OrderTable).LEFT_FOLLOW(OrderTable.customer_id)
        assert "LEFT JOIN customers ON" in db.last_sql
        assert "orders.customer_id = customers.id" in db.last_sql

    async def test_follow_returns_tuple(self):
        """FOLLOW() result should be a tuple of (source, target) objects."""
        db = FakeDB(rows=[(1, 10, 99.99, 10, "Alice")])
        results = await storm.SELECT(db).FROM(OrderTable).FOLLOW(OrderTable.customer_id)
        assert len(results) == 1
        order, customer = results[0]
        assert isinstance(order, Order)
        assert isinstance(customer, Customer)
        assert order.customer_id == 10
        assert customer.name == "Alice"

    async def test_left_follow_null_returns_none(self):
        """LEFT_FOLLOW() with all-NULL joined columns returns None for the target."""
        db = FakeDB(rows=[(1, None, 99.99, None, None)])
        results = await (
            storm.SELECT(db).FROM(OrderTable).LEFT_FOLLOW(OrderTable.customer_id)
        )
        assert len(results) == 1
        order, customer = results[0]
        assert isinstance(order, Order)
        assert customer is None

    async def test_follow_non_fk_raises(self):
        """FOLLOW() on a non-FK column raises ValueError."""
        db = FakeDB()
        with pytest.raises(ValueError, match="not a foreign key"):
            await storm.SELECT(db).FROM(OrderTable).FOLLOW(OrderTable.amount)

    async def test_follow_chaining_with_where(self):
        """FOLLOW() can be chained with WHERE."""
        db = FakeDB(rows=[])
        await (
            storm.SELECT(db)
            .FROM(OrderTable)
            .FOLLOW(OrderTable.customer_id)
            .WHERE(OrderTable.amount > 100)
        )
        assert "INNER JOIN customers ON" in db.last_sql
        assert "WHERE" in db.last_sql
        assert db.last_params == [100]

    def test_follow_sql_method(self):
        """FOLLOW() works with .sql() for inspection."""
        db = FakeDB()
        sql, params = (
            storm.SELECT(db).FROM(OrderTable).FOLLOW(OrderTable.customer_id).sql()
        )
        assert "INNER JOIN customers ON" in sql
        assert "orders.customer_id = customers.id" in sql
        assert params == []

    def test_left_follow_sql_method(self):
        """LEFT_FOLLOW() works with .sql() for inspection."""
        db = FakeDB()
        sql, params = (
            storm.SELECT(db)
            .FROM(OrderTable)
            .LEFT_FOLLOW(OrderTable.customer_id)
            .sql()
        )
        assert "LEFT JOIN customers ON" in sql
        assert params == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `just test`
Expected: FAIL — `SelectBuilder` has no `FOLLOW` or `LEFT_FOLLOW` methods.

- [ ] **Step 3: Implement `FOLLOW()` and `LEFT_FOLLOW()` on `SelectBuilder`**

Add to `SelectBuilder` in `storm/builders.py`, after the `LEFT_JOIN` method:

```python
    def FOLLOW(self, fk_column: Any) -> SelectBuilder:  # noqa: N802
        """INNER JOIN the table that fk_column references, using the FK relationship."""
        return self._follow("INNER", fk_column)

    def LEFT_FOLLOW(self, fk_column: Any) -> SelectBuilder:  # noqa: N802
        """LEFT JOIN the table that fk_column references, using the FK relationship."""
        return self._follow("LEFT", fk_column)

    def _follow(self, join_type: str, fk_column: Any) -> SelectBuilder:
        from .proxy import ColumnProxy, TableProxy

        if not isinstance(fk_column, ColumnProxy):
            raise ValueError(f"{fk_column!r} is not a column proxy")

        field = fk_column._field
        if field.foreign_key is None:
            raise ValueError(
                f"{fk_column._table._meta.cls.__name__}.{field.attr_name} "
                f"is not a foreign key"
            )

        target_proxy = TableProxy(field.foreign_key.target)
        target_pk_col = getattr(target_proxy, target_proxy._meta.pk.attr_name)
        on_predicate = fk_column == target_pk_col
        self._joins.append((join_type, target_proxy, on_predicate))
        return self
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `just test`
Expected: All tests pass.

- [ ] **Step 5: Commit**

```bash
git add storm/builders.py tests/test_builders.py
git commit -m "feat: add FOLLOW() and LEFT_FOLLOW() builder methods on SelectBuilder"
```

---

### Task 6: Update CLAUDE.md and run full check

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Add FK documentation to CLAUDE.md**

In the "Key patterns" section of `CLAUDE.md`, add after the `storm.ops()` bullet:

```markdown
- **`ForeignKey(TargetClass)`** — annotation declaring a foreign key. Always targets the PK of the referenced class. Validated at introspection time: target must be a dataclass with a PK, types must match, field can't be both PK and FK.
- **`storm.follow(db, obj, T.fk_col)`** — loads the related object a FK points to. Returns `None` if FK value is `None` or no matching row exists.
- **`FOLLOW(T.fk_col)` / `LEFT_FOLLOW(T.fk_col)`** — builder methods on `SelectBuilder`. Syntactic sugar for `JOIN` / `LEFT_JOIN` with auto-generated ON condition from FK metadata. Returns tuples like manual JOINs.
```

- [ ] **Step 2: Run full check suite**

Run: `just check`
Expected: fmt-check, lint, typecheck, and all unit tests pass.

- [ ] **Step 3: Fix any issues found by check**

If ruff or mypy report issues, fix them. Re-run `just check` until clean.

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: add ForeignKey, follow(), FOLLOW/LEFT_FOLLOW to CLAUDE.md"
```

---

### Task 7: Integration tests (if STORM_TEST_DSN available)

**Files:**
- Modify: `tests/integration/test_roundtrip.py`

- [ ] **Step 1: Write integration tests**

Add to `tests/integration/test_roundtrip.py`:

```python
@pytest.mark.integration
class TestForeignKeyRoundtrip:
    async def test_follow_loads_related_object(self, db):
        """follow() should load the related object from the database."""
        @dataclasses.dataclass
        class Author:
            id: Annotated[int, DBKey]
            name: str

        @dataclasses.dataclass
        class Book:
            id: Annotated[int, DBKey]
            author_id: Annotated[int, storm.ForeignKey(Author)]
            title: str

        AuthorTable = storm.Table(Author)
        BookTable = storm.Table(Book)

        await db.execute(
            "CREATE TEMP TABLE authors (id SERIAL PRIMARY KEY, name TEXT NOT NULL)", []
        )
        await db.execute(
            "CREATE TEMP TABLE books ("
            "id SERIAL PRIMARY KEY, "
            "author_id INT NOT NULL REFERENCES authors(id), "
            "title TEXT NOT NULL"
            ")",
            [],
        )

        author = Author(id=None, name="Ursula K. Le Guin")
        await storm.create(db, author)

        book = Book(id=None, author_id=author.id, title="The Left Hand of Darkness")
        await storm.create(db, book)

        loaded_author = await storm.follow(db, book, BookTable.author_id)
        assert loaded_author is not None
        assert loaded_author.name == "Ursula K. Le Guin"
        assert loaded_author.id == author.id

    async def test_follow_builder_join(self, db):
        """FOLLOW() in a SELECT builder should produce correct joined results."""
        @dataclasses.dataclass
        class Author2:
            id: Annotated[int, DBKey]
            name: str

        @dataclasses.dataclass
        class Book2:
            id: Annotated[int, DBKey]
            author_id: Annotated[int, storm.ForeignKey(Author2)]
            title: str

        Author2Table = storm.Table(Author2)
        Book2Table = storm.Table(Book2)

        await db.execute(
            "CREATE TEMP TABLE author2s (id SERIAL PRIMARY KEY, name TEXT NOT NULL)", []
        )
        await db.execute(
            "CREATE TEMP TABLE book2s ("
            "id SERIAL PRIMARY KEY, "
            "author_id INT NOT NULL REFERENCES author2s(id), "
            "title TEXT NOT NULL"
            ")",
            [],
        )

        author = Author2(id=None, name="Octavia Butler")
        await storm.create(db, author)

        book = Book2(id=None, author_id=author.id, title="Kindred")
        await storm.create(db, book)

        results = await (
            storm.SELECT(db).FROM(Book2Table).FOLLOW(Book2Table.author_id)
        )
        assert len(results) == 1
        loaded_book, loaded_author = results[0]
        assert loaded_book.title == "Kindred"
        assert loaded_author.name == "Octavia Butler"
```

- [ ] **Step 2: Run integration tests**

Run: `just test-all`
Expected: All tests pass (including new integration tests).

- [ ] **Step 3: Commit**

```bash
git add tests/integration/test_roundtrip.py
git commit -m "test: add FK follow() and FOLLOW() integration tests"
```
