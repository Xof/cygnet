# annotations.py — Marker types carried inside Annotated[] type hints.
#
# These are passive metadata: they don't alter the dataclass at decoration
# time. Instead, meta.py reads them back via get_type_hints(include_extras=True)
# during introspection. Keeping them as plain frozen dataclasses (rather than
# enums or strings) lets us use isinstance() checks unambiguously, even when
# multiple annotation objects coexist in the same Annotated[] bracket.

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class _PrimaryKey:
    # "db" means the database assigns the value (e.g., SERIAL / IDENTITY);
    # "app" means the application must supply it before INSERT.
    # This distinction drives two behaviours in executor.py:
    #   1. DBKey fields with value None are omitted from INSERT column lists
    #      and the generated SQL includes RETURNING <pk>.
    #   2. AppKey fields with value None raise at insert time — there's no
    #      server-side default to fall back on.
    assigned_by: str


# Module-level singletons. Because _PrimaryKey is frozen, these are
# compared by value (==) in meta.py and executor.py, not by identity.
DBKey = _PrimaryKey(assigned_by="db")
AppKey = _PrimaryKey(assigned_by="app")


@dataclass(frozen=True)
class _Column:
    # When None, the Python attribute name is used as the column name.
    name: str | None = None


def Column(name: str) -> _Column:  # noqa: N802
    # Factory function so users write Column("col") rather than _Column("col").
    # The leading underscore on the class signals that it's internal; only
    # this function and the two PK singletons above are public API.
    return _Column(name=name)


@dataclass(frozen=True)
class _ForeignKey:
    # The target dataclass whose primary key this field references.
    # Resolved lazily by meta.py via get_type_hints(include_extras=True),
    # so forward references and circular imports are not a problem.
    target: type


def ForeignKey(target: type) -> _ForeignKey:  # noqa: N802
    # Factory function matching the Column() / DBKey / AppKey pattern.
    # Users write ForeignKey(Customer), not _ForeignKey(target=Customer).
    return _ForeignKey(target=target)


def table(name: str) -> Any:
    """Class decorator: @cygnet.table("my_table_name")"""

    # Stamps a dunder on the class that meta.py checks before falling back to
    # the default naming convention (lowercase class name + "s").  The decorator
    # returns the class unmodified — no wrapper, no metaclass — so dataclass
    # semantics are fully preserved.
    def decorator(cls: Any) -> Any:
        cls.__cygnet_table__ = name
        return cls

    return decorator
