---
id: 0005
title: F5. Python 3.12+ as the floor
date: 2026-04-29
status: Accepted
summary: Python 3.12 is the floor so PEP 695 generic syntax can be used throughout.
---

# 0005. F5. Python 3.12+ as the floor

Type hints use PEP 695 generic syntax (`class TableProxy[T]:`). Python 3.12 is the floor; 3.13 is in the CI matrix.

**Why:** PEP 695 generics are cleaner than `TypeVar` declarations; structural Protocols (`SQLRenderable`) work the same on 3.12+ as later versions. Cutting off 3.11 and earlier costs nothing for a new library and keeps the codebase modern.
