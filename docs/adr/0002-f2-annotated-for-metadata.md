---
id: 0002
title: F2. `Annotated[]` for metadata
date: 2026-04-29
status: Accepted
summary: PK, FK, and column-name metadata ride inside Annotated[T, ...] as passive frozen markers rather than custom field constructors.
---

# 0002. F2. `Annotated[]` for metadata

PK, FK, and column-name overrides live inside `Annotated[T, ...]` annotations:

```python
id: Annotated[int, cygnet.DBKey]
user_id: Annotated[int, cygnet.ForeignKey(User)]
created_at: Annotated[datetime, cygnet.Column("createdAt")]
```

The metadata objects (`_PrimaryKey`, `_ForeignKey`, `_Column`) are passive frozen dataclasses; `meta.py:_introspect` scans them with `isinstance` checks.

**Why:** Standard Python machinery — no class decorators that mutate the dataclass, no string-keyed `Field(...)` constructors. `Annotated` is the type system's official escape hatch for "preserve metadata that mypy/runtime can both see," which is exactly the problem here.

**Trade-off:** The user repeats `Annotated[int, DBKey]` rather than writing `id: DBKey[int]`. We accept the verbosity in exchange for not inventing custom subscriptable generics.

## Addendum (2026-09-27)

A primary key may be annotated `Annotated[int | None, DBKey]` as well as
`Annotated[int, DBKey]`, and the two are equivalent as far as column type and
foreign-key matching are concerned.

The nullable spelling is the more precise one: a `DBKey` attribute genuinely is
`None` between construction and `INSERT`, and that is exactly what lets
`INSERT` omit the column and populate it from `RETURNING` — contrast the
`AppKey` rule per S2, where `None` is an error rather than a state.
`Annotated[int, DBKey]` remains
the documented style because it is shorter, not because the other is wrong.

This was not a new decision so much as a latent ambiguity in F2 that the
implementation had resolved the wrong way: the FK type check unwrapped
`Optional` on the referencing field only, so a non-null FK targeting a nullable
PK was rejected as a type mismatch. Both sides are now unwrapped, so it is the
base types that are compared. Consequences worth keeping in view:

- Unwrapping both sides does not weaken the check — `str | None` against
  `int | None` still raises, because the comparison is on the unwrapped types.
- Wider unions (`int | str`) are deliberately left intact rather than
  unwrapped, since there is no single base type to compare them against. They
  therefore reach the error message as `types.UnionType`.
- Which is why type names in these messages go through a union-safe renderer:
  `types.UnionType` has no `__name__`, and formatting one with `.__name__`
  raised `AttributeError` *while building the diagnostic*, destroying the
  message that would have named the model and field. F2's choice to carry
  metadata inside `Annotated[T, ...]` means `T` is arbitrary type syntax, not
  necessarily a class, so any code that renders a field's type for a human has
  to assume it may be a union or a parameterised generic.
