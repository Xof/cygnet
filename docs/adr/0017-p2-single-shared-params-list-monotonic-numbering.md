---
id: 0017
title: P2. Single shared params list, monotonic numbering
date: 2026-04-29
status: Accepted
summary: One params list is shared across the whole render pass, so monotonic $N numbering falls out of document-order rendering.
---

# 0017. P2. Single shared params list, monotonic numbering

`Executor._render_select(b, params)` and every renderable's `render_sql(params)` take the SAME list. Subqueries, CTEs, lateral joins, set operations, and the outer WHERE clause all append to it in document order. Number = `len(params)` after append.

**Why:** Two-pass rendering (first collect params, then format SQL) would force each renderable to know its position ahead of time. Single-pass with a shared list means each renderable can act locally; positional numbering falls out for free as long as everyone renders in textual order.
