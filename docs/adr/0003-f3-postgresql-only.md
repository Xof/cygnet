---
id: 0003
title: F3. PostgreSQL only
date: 2026-04-29
status: Accepted
summary: PostgreSQL syntax is emitted verbatim with no dialect abstraction, and the decision is final rather than a staging post.
---

# 0003. F3. PostgreSQL only

No dialect abstraction, no SQL standard subset, no portability shims. Cygnet emits PostgreSQL syntax verbatim: `$N` placeholders, `ON CONFLICT`, `RETURNING`, `DISTINCT ON`, `WITH RECURSIVE`, `LATERAL`, `FOR UPDATE OF`, `tsvector @@ tsquery`, JSONB operators (`->`, `->>`, `@>`).

**Why:** Multi-dialect abstraction layers force lowest-common-denominator features and add complexity the target audience doesn't want. Users who chose PostgreSQL did so for these features; hiding them defeats the purpose. The decision is final, not a "we'll add MySQL later" stance.
