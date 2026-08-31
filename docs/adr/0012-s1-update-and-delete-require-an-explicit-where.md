---
id: 0012
title: S1. `UPDATE` and `DELETE` require an explicit `WHERE`
date: 2026-04-29
status: Accepted
summary: UPDATE and DELETE raise without an explicit WHERE, with cygnet.all as the deliberate opt-out.
---

# 0012. S1. `UPDATE` and `DELETE` require an explicit `WHERE`

Both builders raise `ValueError` at execute time (and at `.sql()` time) if no `.WHERE()` was called. `cygnet.all` is the explicit opt-out: `DELETE(db).FROM(T).WHERE(cygnet.all)`.

**Why:** Mass-mutation accidents are the most expensive class of SQL mistake. The cost of typing `WHERE(cygnet.all)` is two words; the cost of `DELETE FROM users` without a WHERE is a job interview. Mixing `cygnet.all` with real predicates also raises — combining "all rows" with a filter is contradictory.
