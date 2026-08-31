---
id: 0014
title: S3. `DBKey` + `frozen=True` rejected at introspection time
date: 2026-04-29
status: Accepted
summary: DBKey plus frozen=True is rejected at introspection so the error names the model instead of failing at a later setattr.
---

# 0014. S3. `DBKey` + `frozen=True` rejected at introspection time

After `INSERT … RETURNING`, the executor `setattr`s the generated PK onto the in-memory object. Frozen dataclasses raise `FrozenInstanceError`. We catch this at `_introspect` rather than at insert time so the error names the model class, not the mysterious internal setattr line.
