---
id: 0021
title: C1. `WeakValueDictionary` singletons for `TableMeta` and `TableProxy`
date: 2026-04-29
status: Accepted
summary: TableMeta and TableProxy are per-class singletons in WeakValueDictionary caches, so identity comparisons hold without pinning model classes.
---

# 0021. C1. `WeakValueDictionary` singletons for `TableMeta` and `TableProxy`

Both classes implement a `__new__` + `_initialised` flag pattern that returns the cached instance for a given dataclass. The caches are `weakref.WeakValueDictionary` so a model class that goes out of scope (e.g., declared inside a test function) takes its meta/proxy with it.

**Why:** `cygnet.Table(MyModel)` is `cygnet.Table(MyModel)` — identity comparison (`b._table is X`) works elsewhere in the codebase because the proxy is the same object. Plain `dict` caching would pin every model class ever wrapped for the lifetime of the process, a slow leak for codebases that dynamically generate dataclasses (codegen, tests).
