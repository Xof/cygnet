# Foreign Key Declaration and Follow

## Summary

Add foreign key awareness to Storm so that users can declare FK relationships in annotations and use shorthand APIs to load related objects — without building JOIN conditions by hand.

## Motivation

Today, Storm models can have integer fields that happen to be foreign keys, but the ORM has no knowledge of the relationship. Loading a related object requires the user to manually construct a `get()` call or write an explicit JOIN with the correct ON condition. This is repetitive and error-prone for the most common relational pattern: "I have this row, give me the row it points to."

## Design

### Annotation: `ForeignKey(target_class)`

A new annotation marker, following the same pattern as `DBKey`, `AppKey`, and `Column()`.

```python
from storm import ForeignKey

@dataclass
class Customer:
    id: Annotated[int, DBKey]
    name: str

@dataclass
class Order:
    id: Annotated[int, DBKey]
    customer_id: Annotated[int, ForeignKey(Customer)]
    amount: float
```

`ForeignKey` is a public factory function that returns a `_ForeignKey` frozen dataclass instance. It always targets the primary key of the referenced class.

**Implementation in `annotations.py`:**

```python
@dataclass(frozen=True)
class _ForeignKey:
    target: type
```

`ForeignKey(cls)` returns `_ForeignKey(target=cls)`.

### Metadata: `FieldMeta.foreign_key`

`FieldMeta` gains an optional `foreign_key: _ForeignKey | None` field (default `None`).

`_introspect()` scans `Annotated` extras for `_ForeignKey` in the same loop that finds `_PrimaryKey` and `_Column`.

**Validation at introspection time:**

- The target class must be a dataclass with a Storm primary key defined.
- A field cannot be both a primary key and a foreign key.
- The Python type of the FK field must match the Python type of the target's primary key.

`TableMeta` gains a convenience property:

```python
@property
def foreign_keys(self) -> list[FieldMeta]:
    return [f for f in self.fields if f.foreign_key is not None]
```

### Standalone function: `storm.follow()`

```python
async def follow(db, obj, fk_column: ColumnProxy) -> object | None
```

**Usage:**

```python
order = await storm.get(db, OrderTable, id=42)
customer = await storm.follow(db, order, OrderTable.customer_id)
```

**Behavior:**

1. Resolve `fk_column` to its `FieldMeta`. Validate it has a `foreign_key`.
2. Read the FK value from `obj` using the field's `attr_name`.
3. If the value is `None`, return `None` without hitting the database.
4. Otherwise, call `get(db, target_table_proxy, **{pk_attr_name: fk_value})` internally.

**Error cases:**

- `fk_column` is not a foreign key: `ValueError`.
- `obj` is not an instance of the table the `fk_column` belongs to: `TypeError`.
- No matching row in the target table: returns `None` (same as `get()`).

### Builder methods: `FOLLOW()` / `LEFT_FOLLOW()`

Methods on `SelectBuilder` that generate JOINs from FK metadata.

```python
# INNER JOIN — only orders with a matching customer
results = await (
    storm.SELECT(db)
    .FROM(OrderTable)
    .FOLLOW(OrderTable.customer_id)
)
# list[(Order, Customer)]

# LEFT JOIN — all orders, None where customer is missing
results = await (
    storm.SELECT(db)
    .FROM(OrderTable)
    .LEFT_FOLLOW(OrderTable.customer_id)
)
# list[(Order, Customer | None)]
```

**Implementation:**

`FOLLOW(fk_column)` and `LEFT_FOLLOW(fk_column)`:

1. Resolve the `FieldMeta` for `fk_column`. Validate it has a `foreign_key`.
2. Look up (or create) the `TableProxy` for the target class.
3. Delegate to `.JOIN()` / `.LEFT_JOIN()` with the auto-generated ON condition: `fk_column == target_pk_column`.

No new SQL rendering or row mapping — these methods are syntactic sugar over existing JOIN infrastructure.

**Chaining and mixing:**

Multiple `FOLLOW` calls work like multiple `JOIN` calls:

```python
results = await (
    storm.SELECT(db)
    .FROM(OrderTable)
    .FOLLOW(OrderTable.customer_id)
    .FOLLOW(OrderTable.warehouse_id)
)
# list[(Order, Customer, Warehouse)]
```

`FOLLOW` can be mixed freely with manual `JOIN`, `WHERE`, `ORDER_BY`, `LIMIT`, etc.

## Public API

| Name | Kind | Description |
|------|------|-------------|
| `ForeignKey(target_class)` | Annotation factory | Declares a FK relationship |
| `follow(db, obj, fk_column)` | Async function | Loads the related object for one instance |
| `SelectBuilder.FOLLOW(fk_column)` | Builder method | INNER JOIN via FK |
| `SelectBuilder.LEFT_FOLLOW(fk_column)` | Builder method | LEFT JOIN via FK |

## Files changed

| File | Change |
|------|--------|
| `annotations.py` | Add `_ForeignKey` dataclass, `ForeignKey()` factory |
| `meta.py` | Add `foreign_key` to `FieldMeta`, scan in `_introspect()`, validation, `foreign_keys` property |
| `builders.py` | Add `FOLLOW()` and `LEFT_FOLLOW()` to `SelectBuilder` |
| `__init__.py` | Add `follow()` function, export `ForeignKey` and `follow` |
| `expression.py` | No changes |
| `predicate.py` | No changes |
| `proxy.py` | No changes |
| `executor.py` | No changes |

## Out of scope

- Cascade delete/update behavior (DDL/migration concern, not ORM query concern)
- Reverse FK traversal ("all orders for this customer") — already served by `WHERE`
- Composite foreign keys
- FK to non-PK columns
