---
id: 0033
title: Open questions (from 2026-04-29 review)
date: 2026-04-29
status: Proposed
summary: Running list of four unresolved questions (OQ1-OQ4) from the 2026-04-29 fresh-eyes review.
---

# 0033. Open questions (from 2026-04-29 review)

Not blockers; recorded so future work has the running list:

- **OQ1.** `save()` upsert RETURNING — deliberate trade-off or oversight? Affects in-memory object refresh on upsert path.
- **OQ2.** `op()` 1-arg factory-factory usage — is it earning its keep?
- **OQ3.** Regex-based `$N` → `%s` translation footgun for `lit()` — document explicitly in the lit() docstring or accept silently?
- **OQ4.** Promote `cte._meta` / TableProxy duck-typed surface to a real `TableSourceProtocol` to remove the `# type: ignore` annotations.

Full review at `docs/reviews/review-20260429-175335.md`.
