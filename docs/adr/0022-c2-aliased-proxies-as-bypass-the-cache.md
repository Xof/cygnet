---
id: 0022
title: C2. Aliased proxies (`.AS()`) bypass the cache
date: 2026-04-29
status: Accepted
summary: AS(alias) returns a fresh proxy outside the cache so self-joins get distinct proxies while the canonical one stays a singleton.
---

# 0022. C2. Aliased proxies (`.AS()`) bypass the cache

`T.AS("alias")` returns a fresh `TableProxy` via `object.__new__` — it does not consult the cache. The canonical `Table(cls)` lookup remains a singleton.

**Why:** Self-joins (same table referenced twice) require two distinct proxies with different aliases. The canonical proxy stays singleton so identity comparisons elsewhere keep working; aliased proxies are caller-scoped.
