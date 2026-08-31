---
id: 0006
title: A1. UPPER_CASE for SQL verbs, lower_case for Python utilities
date: 2026-04-29
status: Accepted
summary: SQL-keyword methods are UPPER_CASE and Python helpers lower_case, so call sites visually separate the two.
---

# 0006. A1. UPPER_CASE for SQL verbs, lower_case for Python utilities

Method names that mirror SQL keywords are uppercase: `SELECT`, `INSERT`, `FROM`, `WHERE`, `JOIN`, `ORDER_BY`, `GROUP_BY`, `LIMIT`, `OFFSET`, `RETURNING`, `WITH`, `UNION`, `DISTINCT_ON`, `FOR_UPDATE`. Python utilities are lowercase: `cygnet.get`, `cygnet.save`, `cygnet.create`, `cygnet.follow`, `cygnet.transaction`, `cygnet.lit`, `cygnet.op`, `cygnet.exists`.

**Why:** Visually distinguishes SQL keywords from Python helpers at the call site, which is the whole purpose of the library. PEP 8 violations are silenced with `# noqa: N802` per method; the lint rule is right in general and intentionally violated here.
