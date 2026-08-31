---
id: 0013
title: S2. `AppKey` + `None` raises at INSERT
date: 2026-04-29
status: Accepted
summary: An AppKey field that is None at INSERT raises rather than silently writing NULL.
---

# 0013. S2. `AppKey` + `None` raises at INSERT

If a model uses `Annotated[T, AppKey]` and the value is `None` when INSERT runs, the executor raises `ValueError` rather than silently passing `NULL`. `DBKey` + `None`, by contrast, is the normal case (the database generates the value).

**Why:** AppKey means "the application supplies the key" — `None` is unambiguously an error.
