---
id: 0009
title: A4. `ColumnProxy.__eq__` returns `Predicate`, not `bool`
date: 2026-04-29
status: Accepted
summary: ColumnProxy.__eq__ returns a Predicate AST node instead of a bool, at the cost of making the proxy unhashable.
---

# 0009. A4. `ColumnProxy.__eq__` returns `Predicate`, not `bool`

`T.id == 1` is a syntactic trick: `ColumnProxy.__eq__` is overridden to return a `Predicate` AST node, not a Python boolean. The same applies to `__ne__`, `__lt__`, `__gt__`, `__le__`, `__ge__`, and arithmetic operators (`+`, `-`, `*`, `/`, `%`, all with `__r*` variants).

**Why:** `T.id == 1` is the most readable way to express a SQL equality. Without operator overloading, the user would write `T.id.eq(1)` or `Predicate(T.id, "=", 1)` — neither flows the same way.

**Cost:** `ColumnProxy.__hash__` must be set to `None` because Python auto-generates `__hash__` from `__eq__`, and our `__eq__` no longer satisfies the hash-equality invariant. The class becomes unhashable, which prevents subtle bugs (a proxy in a set or as a dict key would behave nonsensically).

**Same trick** is used on `Predicate`, `FunctionCall`, `WindowExpression`, `PrefixOp`, `SuffixOp`, and `_Exists`. The whole expression tree is `__eq__`-overloaded.
