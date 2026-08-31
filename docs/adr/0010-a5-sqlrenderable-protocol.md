---
id: 0010
title: A5. `SQLRenderable` protocol
date: 2026-04-29
status: Accepted
summary: Every renderable implements one method, render_sql(params) -> str, appending its values to a shared params list.
---

# 0010. A5. `SQLRenderable` protocol

Every type that can appear in a query has a single method: `render_sql(params: list[Any]) -> str`. The method mutates `params` (appending values, returning `$N` placeholders) and returns the SQL fragment. Duck-typed at use sites with `hasattr(value, "render_sql")`.

**Why:** A single protocol lets new expression types (window functions, JSONB operators, full-text search, lateral joins, subqueries) participate in WHERE / ORDER BY / SELECT-list / HAVING positions without any wrapping. The protocol is structural; users can implement their own SQL fragments and pass them in.

**Subtle point:** `params` is shared across the entire render pass. `$N` numbering is monotonic across all clauses because every renderable appends to the same list in document order. This is what makes `SELECT col, (subquery1) FROM T WHERE x IN (subquery2) AND y = 5` produce coherent numbering — the subqueries' params come in textual order.
