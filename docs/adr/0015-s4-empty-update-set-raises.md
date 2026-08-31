---
id: 0015
title: S4. Empty `UPDATE … SET` raises
date: 2026-04-29
status: Accepted
summary: An empty UPDATE SET raises instead of emitting a silent no-op, and unknown kwargs are rejected alongside it.
---

# 0015. S4. Empty `UPDATE … SET` raises

`UPDATE(db).SET(T).WHERE(...)` (no kwargs, no obj) used to silently emit an empty SET clause. Now raises `ValueError("UPDATE SET requires at least one field")` at render time. Also catches typos in `.SET(T, nmae="x")` via the unknown-kwargs check (same rule on INSERT and ON CONFLICT DO UPDATE).

**Why:** Silent no-ops were the "dangerous kind of safety rail" — they masked bugs (typo'd field names) rather than catching them.
