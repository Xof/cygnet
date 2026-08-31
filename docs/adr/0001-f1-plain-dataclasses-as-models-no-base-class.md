---
id: 0001
title: F1. Plain dataclasses as models (no base class)
date: 2026-04-29
status: Accepted
summary: Models are plain dataclasses with no base class, metaclass, or manager; metadata comes from Annotated[] introspection.
---

# 0001. F1. Plain dataclasses as models (no base class)

Models are vanilla `@dataclasses.dataclass` classes. There is no `cygnet.Model` to inherit from, no metaclass, no `Manager` attribute, no `objects` proxy. Cygnet introspects the dataclass via `typing.get_type_hints(..., include_extras=True)` to discover columns and PK markers.

**Why:** Subclassing a framework class couples the model to Cygnet at import time and makes models hard to use outside ORM contexts (serialisation, validation, business logic in pure-Python tests, etc.). Plain dataclasses round-trip through `dataclasses.asdict`, pickle, copy, and any other tool that knows the stdlib protocol. Reaching for Cygnet is opt-in per call site, not per model.

**Cost:** The model class can't carry helper methods that need a `db` — the user wires those in their own service layer. We consider this a feature.
