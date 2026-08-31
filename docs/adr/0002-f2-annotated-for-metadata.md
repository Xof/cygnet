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
