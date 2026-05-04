# cte.py — Common-table-expression support: WITH name AS (…).
#
# A CTE is a named subquery that can be FROMed, JOINed, and have its
# columns referenced.  This module's CTE class duck-types enough of
# TableProxy's interface (`_sql_name`, `_meta.table_name`, `_meta.fields`,
# `_alias`) that the executor can render `FROM cte_name` / `JOIN cte_name
# ON …` paths without special-casing.  Column attributes are stamped at
# construction time so `cte.id == 5` returns a Predicate, just like
# `T.id == 5` on a regular table.
#
# Recursive CTEs (`WITH RECURSIVE …`) are deliberately out of scope for
# this initial pass; revisit once a real use case appears.

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class _PseudoField:
    """A FieldMeta-shaped record for CTE columns.

    CTE columns aren't tied to a dataclass field; they're just names.
    The executor only reads attr_name / column_name / primary_key /
    foreign_key, so this minimal shape is enough to satisfy it.
    """

    attr_name: str
    column_name: str
    # Defaults match the "ordinary value column" case used everywhere a
    # CTE column is consumed.  CTE-from-SELECT introspection doesn't try
    # to recover real PK/FK metadata — everything is treated as
    # non-keyed, non-foreign.
    primary_key: Any = None
    foreign_key: Any = None
    python_type: type = type(None)


class CTE:
    """A WITH-clause CTE bound to a name + inner SelectBuilder.

    Construct via cygnet.cte(name, inner) or cygnet.cte(name, inner,
    columns=[...]).  Column names are inferred from the inner builder
    when possible (explicit ColumnProxy columns, or a bare SELECT FROM
    T fall-back to T's fields), or supplied explicitly when the inner
    SELECT uses expressions whose names we can't recover.
    """

    def __init__(
        self,
        name: str,
        builder: Any,
        columns: list[str] | None = None,
    ) -> None:
        self._name = name
        self._builder = builder
        self._cols = self._resolve_columns(builder, columns)
        # _alias mirrors TableProxy's alias attribute so executor code that
        # checks `if jt._alias:` works against a CTE without crashing.
        # CTEs don't currently support .AS() — the WITH name is the alias.
        self._alias: str | None = None
        # Stamp ColumnProxy-like attributes so `cte.id == 5` works.  Lazy-
        # imported to avoid the circular dep proxy → predicate → cte.
        from .proxy import ColumnProxy

        for col in self._cols:
            field = _PseudoField(attr_name=col, column_name=col)
            # Duck-typing: ColumnProxy reads `_sql_name` off its table
            # back-ref and `column_name` off its field.  CTE / _PseudoField
            # provide both, but the static types don't formally match
            # TableProxy / FieldMeta.  The ignore is intentional.
            setattr(self, col, ColumnProxy(self, field))  # type: ignore[arg-type]

    def _resolve_columns(self, builder: Any, columns: list[str] | None) -> list[str]:
        if columns is not None:
            return list(columns)
        from .proxy import ColumnProxy

        # Bare SELECT(db).FROM(T): inherit the model's column names so
        # `WITH x AS (SELECT * FROM T) SELECT x.id FROM x` works.
        if not builder._columns:
            if builder._table is None:
                raise ValueError(
                    "CTE column inference needs the inner SELECT to have "
                    "either explicit columns or a FROM(T) — neither found"
                )
            return [f.attr_name for f in builder._table._meta.fields]
        # Explicit column list: each must be a ColumnProxy whose attr name
        # we can read off.  Anything else (lit / op / fn) is opaque and
        # the user must supply columns=[...] explicitly.
        names: list[str] = []
        for c in builder._columns:
            if isinstance(c, ColumnProxy):
                names.append(c._field.attr_name)
            else:
                raise ValueError(
                    f"CTE column inference can't determine a name for "
                    f"{c!r}; pass columns=[...] to cygnet.cte() explicitly"
                )
        return names

    # ── TableProxy-shaped surface used by the executor ──────────────────

    @property
    def _sql_name(self) -> str:
        return self._name

    @property
    def _meta(self) -> CTE:
        # CTE is its own "meta" — table_name + fields are read directly
        # off the same instance.  Saves a separate class without losing
        # any structure since CTEs are always fully described by name +
        # column list.
        return self

    @property
    def table_name(self) -> str:
        return self._name

    @property
    def fields(self) -> list[_PseudoField]:
        return [_PseudoField(c, c) for c in self._cols]

    @property
    def pk(self) -> None:
        # CTEs have no primary key.  cygnet.get / save / DBKey paths
        # don't apply to them; SELECT-only consumers don't care.
        return None

    @property
    def cls(self) -> type:
        # Used in error messages where executor code reads meta.cls.__name__.
        return type(self)


def cte(
    name: str,
    builder: Any,
    columns: list[str] | None = None,
) -> CTE:
    """Build a CTE for use in a WITH clause.

    Most calls don't need `columns`: if the inner SelectBuilder uses
    explicit column references (cygnet.SELECT(db, T.id, T.name)) or a
    bare SELECT FROM T, the column names are inferred.  Supply columns
    explicitly when the inner SELECT uses opaque expressions like
    cygnet.fn(...) or cygnet.lit(...).
    """
    return CTE(name, builder, columns)


class RecursiveCTE:
    """A `WITH RECURSIVE name(cols) AS (anchor UNION ALL step)` block.

    Recursive CTEs reference themselves in the recursive step, so the
    CTE proxy must exist BEFORE its bodies do.  The expected pattern:

        c = cygnet.recursive_cte("counter", columns=["n"])
        c.anchor = cygnet.SELECT(db, cygnet.lit("1"))
        c.step = (
            cygnet.SELECT(db, c.n + 1)
            .FROM(c)
            .WHERE(c.n < 10)
        )
        rows = await cygnet.SELECT(db, c.n).WITH(c).FROM(c)

    `columns` is required (unlike non-recursive CTEs that can infer):
    the recursive step needs column refs available at the time it's
    built, before either body is committed to a known shape.

    Recursive CTEs share the TableProxy-shaped duck-type surface with
    CTE so they can sit in FROM / JOIN / outer-WHERE the same way.
    """

    def __init__(self, name: str, columns: list[str]) -> None:
        if not columns:
            raise ValueError(
                "recursive_cte requires an explicit columns=[...]; "
                "they must be available before the recursive step is built"
            )
        self._name = name
        self._cols = list(columns)
        self._alias: str | None = None
        # User-assigned bodies.  Both must be populated before the CTE
        # is rendered; the executor checks at render time.
        self.anchor: Any = None
        self.step: Any = None
        # Stamp column proxies just like CTE does; the same duck-typed
        # ColumnProxy(table=self, field=_PseudoField(...)) trick.
        from .proxy import ColumnProxy

        for col in self._cols:
            field = _PseudoField(attr_name=col, column_name=col)
            setattr(self, col, ColumnProxy(self, field))  # type: ignore[arg-type]

    # ── TableProxy-shaped surface used by the executor ──────────────────
    # Identical to CTE's; duplicated rather than refactored into a base
    # because the two classes have meaningfully different internal
    # state (single builder vs. anchor + step) and refactoring would
    # introduce a diamond / mixin pattern for trivial savings.

    @property
    def _sql_name(self) -> str:
        return self._name

    @property
    def _meta(self) -> RecursiveCTE:
        return self

    @property
    def table_name(self) -> str:
        return self._name

    @property
    def fields(self) -> list[_PseudoField]:
        return [_PseudoField(c, c) for c in self._cols]

    @property
    def pk(self) -> None:
        return None

    @property
    def cls(self) -> type:
        return type(self)


def recursive_cte(name: str, columns: list[str]) -> RecursiveCTE:
    """Build a forward-declared recursive CTE.

    Returns a RecursiveCTE whose column refs are available immediately
    so the recursive step can reference them.  Set `.anchor` and `.step`
    before using the CTE in an outer SELECT.
    """
    return RecursiveCTE(name, columns)


class Lateral(CTE):
    """A LATERAL subquery — `LATERAL (inner_sql) alias` — usable as the
    right side of a JOIN where the inner SELECT can correlate to columns
    from preceding FROM/JOIN tables.

    Structurally identical to CTE (name + inner builder + column refs),
    so we subclass: same `_sql_name`, `_meta`, column-proxy stamping.
    The placement difference (FROM/JOIN vs WITH-clause) is handled by
    the executor via `isinstance(jt, Lateral)` when rendering joins.

    Inner-builder columns are inferred the same way CTE infers them
    (explicit ColumnProxy refs, or bare SELECT FROM T → all of T's
    fields).  Pass `columns=[...]` explicitly when the inner SELECT
    projects opaque expressions.
    """


def lateral(
    name: str,
    builder: Any,
    columns: list[str] | None = None,
) -> Lateral:
    """Build a lateral subquery for use in a JOIN_LATERAL / LEFT_JOIN_LATERAL.

    Most calls don't need `columns` — same inference rules as cte().
    The returned object's column attributes (`lat.colname`) work as
    drop-in references in the outer SELECT's WHERE / projection / etc.
    """
    return Lateral(name, builder, columns)
