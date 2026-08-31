---
id: 0025
title: CO3. UPDATE SET right-hand side accepts renderables
date: 2026-04-29
status: Accepted
summary: UPDATE SET accepts renderables on the right-hand side, covering count = count + 1, cross-table updates, and computed values.
---

# 0025. CO3. UPDATE SET right-hand side accepts renderables

`UPDATE(db).SET(T, count=T.count + 1, name=cygnet.fn("upper")(T.name))` works because the SET render branch checks `hasattr(value, "render_sql")` for each kwarg. Renderables render in place; literals become `$N` parameters.

**Why:** `count = count + 1`, cross-table UPDATE … FROM joins (`SET email=Other.value`), and computed updates (`SET name = upper(name)`) all need expression RHS support. Branching at render time keeps the API one method, not two.
