# Operators Design

**Date:** 2026-04-07
**Status:** Approved
**Scope:** Add `storm.op()`, `storm.ops()`, `storm.is_null()`,
`storm.is_not_null()` for SQL operators that don't map to Python comparison
operators. All satisfy `SQLRenderable`.

## Goal

Cover SQL operators beyond `=`, `!=`, `<`, `>`, `<=`, `>=` — things like
`ILIKE`, `NOT`, `IS NULL`, `SIMILAR TO`, `@>`, etc.

## Decisions

| Question | Decision |
|---|---|
| Infix op return type | `Predicate` — reuses existing class directly |
| Prefix/suffix classes | Two separate classes: `PrefixOp`, `SuffixOp` |
| Compound support (`&`/`|`) | Yes — `PrefixOp` and `SuffixOp` have `__and__`/`__or__` returning `Predicate` |
| Module location | `storm/expression.py` alongside `SQLRenderable` |
| Approach | Thin wrappers, `op()` overloaded by arg count |

## Expression Classes

In `storm/expression.py`:

### PrefixOp

```python
@dataclass(frozen=True)
class PrefixOp:
    op: str
    operand: Any

    def render_sql(self, params: list[Any]) -> str:
        return f"{self.op} ({self.operand.render_sql(params)})"

    def __and__(self, other): return Predicate(self, "AND", other)
    def __or__(self, other): return Predicate(self, "OR", other)
```

Renders as `NOT (expr)`. Wraps operand in parens.

### SuffixOp

```python
@dataclass(frozen=True)
class SuffixOp:
    operand: Any
    op: str

    def render_sql(self, params: list[Any]) -> str:
        return f"{self.operand.render_sql(params)} {self.op}"

    def __and__(self, other): return Predicate(self, "AND", other)
    def __or__(self, other): return Predicate(self, "OR", other)
```

Renders as `expr IS NULL`. No parens around operand.

Both are frozen dataclasses. Both satisfy `SQLRenderable`. Both import
`Predicate` from `predicate.py` (no circular dependency — `predicate.py`
does not import from `expression.py`).

## Factory Functions

All in `storm/expression.py`, re-exported via `storm/__init__.py`.

### `op()` — three calling conventions

Disambiguated by argument count (no type inspection needed):

- **3-arg infix:** `storm.op(left, 'ILIKE', right)` →
  `Predicate(left, 'ILIKE', right)`
- **2-arg prefix:** `storm.op('NOT', expr)` →
  `PrefixOp(op='NOT', operand=expr)`
- **1-arg pre-created:** `storm.op('ILIKE')` → returns a callable;
  `ILIKE(left, right)` produces `Predicate(left, 'ILIKE', right)`

Raises `TypeError` on 0 args or 4+ args.

### `ops()` — suffix operator

`storm.ops(operand, operator)` → `SuffixOp(operand=operand, op=operator)`

Always 2 args. No overloading.

### `is_null()` / `is_not_null()` — convenience builtins

```python
def is_null(operand):
    return SuffixOp(operand=operand, op="IS NULL")

def is_not_null(operand):
    return SuffixOp(operand=operand, op="IS NOT NULL")
```

## Public API

New exports in `storm/__init__.py` and `__all__`:

- `storm.op` — infix, prefix, pre-created
- `storm.ops` — suffix
- `storm.is_null` — IS NULL convenience
- `storm.is_not_null` — IS NOT NULL convenience

`PrefixOp` and `SuffixOp` are NOT exported — internal classes.

## Testing

All unit tests, FakeDB.

1. **`op()` infix:** `storm.op(T.name, 'ILIKE', '%fred%')` →
   `accounts.name ILIKE $1`, params `['%fred%']`
2. **`op()` prefix:** `storm.op('NOT', T.active == True)` →
   `NOT (accounts.active = $1)`
3. **`op()` pre-created:** `ILIKE = storm.op('ILIKE'); ILIKE(T.name, '%f%')`
   → same as infix
4. **`ops()` suffix:** `storm.ops(T.email, 'IS NULL')` →
   `accounts.email IS NULL`
5. **`is_null()`:** `storm.is_null(T.email)` →
   `accounts.email IS NULL`
6. **`is_not_null()`:** `storm.is_not_null(T.email)` →
   `accounts.email IS NOT NULL`
7. **Compound:** `storm.is_null(T.email) & (T.name == 'Fred')` →
   renders with AND
8. **In WHERE:** `SELECT(db).FROM(T).WHERE(storm.is_null(T.email))` →
   executes via FakeDB
9. **Wrong args:** `storm.op()` raises `TypeError`
