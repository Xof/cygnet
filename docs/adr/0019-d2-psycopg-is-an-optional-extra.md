---
id: 0019
title: D2. `psycopg` is an optional extra
date: 2026-04-29
status: Accepted
summary: psycopg is an optional [psycopg] extra, keeping the core install driver-free.
---

# 0019. D2. `psycopg` is an optional extra

The reference psycopg adapter lives at `cygnet/psycopg_db.py`. `psycopg[binary]>=3.1` is declared in the `[psycopg]` optional-dependencies group, not in the core `dependencies` list. Importing `cygnet.psycopg_db` without the extra installed raises a clear `ImportError` pointing at `pip install 'cygnet-orm[psycopg]'`.

**Why:** Cygnet's actual code never imports psycopg outside `psycopg_db.py`. Forcing psycopg into the core install would contradict D1 (no driver coupling). The optional extra makes the core install driver-free; users choosing their own adapter never pull psycopg.
