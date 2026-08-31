---
id: 0011
title: A6. `SelectBuilder` itself implements `SQLRenderable`
date: 2026-04-29
status: Accepted
summary: SelectBuilder is itself a renderable that parenthesises its own output, so any builder works as an inline subquery.
---

# 0011. A6. `SelectBuilder` itself implements `SQLRenderable`

`SelectBuilder.render_sql(params)` delegates to `Executor._render_select` and wraps the result in parens. This makes any builder usable as an inline subquery: scalar subquery in a SELECT list, `EXISTS (b)`, `col IN (b)`, etc. No `subquery(b)` wrapper, no special-case verbs.

**Why:** Once a builder is a renderable, every expression context "just works" with no per-context API. `cygnet.exists(b)` is a thin wrapper around the EXISTS keyword; `cygnet.op(T.col, "IN", b)` works because `op` already accepts any renderable as an operand. The parens-on-self rule is the trick that avoids double-parenthesisation downstream.
