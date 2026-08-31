---
id: 0024
title: CO2. `_Exists` is dedicated, not `PrefixOp`-reuse
date: 2026-04-29
status: Accepted
summary: exists() returns a dedicated _Exists node rather than reusing PrefixOp, avoiding double parens and giving canonical NOT EXISTS.
---

# 0024. CO2. `_Exists` is dedicated, not `PrefixOp`-reuse

`cygnet.exists(b)` returns a `_Exists` frozen dataclass, not a `PrefixOp("EXISTS", b)`. `PrefixOp.render_sql` wraps its operand in parens; since `SelectBuilder.render_sql` already wraps in parens (per A6), `PrefixOp` would emit `EXISTS ((SELECT …))` — valid PG, ugly. `_Exists` skips the extra parens.

Also: `~exists(b)` toggles `EXISTS ↔ NOT EXISTS` rather than wrapping in another NOT, producing the canonical anti-join form `NOT EXISTS (…)` rather than `NOT (EXISTS (…))`.
