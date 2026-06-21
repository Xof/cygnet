# proxy.py — TableProxy and ColumnProxy: the user-facing expression API.
#
# T = cygnet.Table(MyModel) creates a TableProxy whose attributes are
# ColumnProxy objects.  Writing T.name == "Fred" returns a Predicate
# (not a bool), because ColumnProxy overrides __eq__.  This is the
# mechanism that makes Cygnet queries read like SQL expressions.
#
# Both proxies are cached per dataclass via WeakValueDictionary, so
# cygnet.Table(Account) always returns the same object.  This is important
# for identity: executor.py and builders.py compare proxies by reference
# (e.g., b._table) when deciding how to render queries.
#
# Aliased proxies (T.AS("a")) are the deliberate exception to the singleton
# rule.  They share their TableMeta with the canonical proxy (introspection
# happens once per dataclass) but live outside the cache so a single query
# can hold two distinct proxies for the same table without aliasing them
# together.

from __future__ import annotations

import weakref
from typing import Any

from .expression import FieldLike, TableSourceProtocol
from .meta import TableMeta
from .predicate import _InfixOps


class ColumnProxy[FT](_InfixOps):
    """A typed reference to one column of a table proxy.

    Generic on FT (the column's Python value type), so explicit
    annotations like `ColumnProxy[str]` carry through to comparison
    helpers and user-defined functions that take a column.  At runtime
    FT is erased; the param is purely for static type checking.

    NOTE on IDE autocomplete: at the call site `T.name` resolves to a
    plain `ColumnProxy` (no field-type inference) because Python's type
    system can't project a generic-parameter class's fields into typed
    proxy attributes without a mypy plugin or codegen.  See ISSUES.md
    item 5.4 for the trade-offs.  Users who want strict typing can spell
    the type explicitly:
        name_col: ColumnProxy[str] = T.name
    """

    def __init__(self, table: TableSourceProtocol, field: FieldLike) -> None:
        # Back-reference to owning table-source is needed for fully-qualified
        # rendering ("table.column") — see render_sql below.  Keeping the
        # back-ref means a ColumnProxy can be passed anywhere an SQLRenderable
        # is expected without needing its parent table as context.
        # S8: the table arg is typed as ``TableSourceProtocol`` (not
        # ``TableProxy[Any]``) so CTE / RecursiveCTE / Lateral satisfy the
        # signature without ``# type: ignore[arg-type]``.  Same for field
        # via FieldLike (covers FieldMeta and _PseudoField).
        self._table = table
        self._field = field

    # Comparison + arithmetic operators and __hash__ = None come from the
    # shared _InfixOps mixin (see predicate.py).  A bare column carries no
    # logical connectives (__and__/__or__/__invert__) — it isn't itself a
    # boolean predicate.

    def render_sql(self, params: list[Any]) -> str:
        # Always renders as "table.column" (fully qualified), which avoids
        # ambiguity in JOINs.  When the parent TableProxy carries an alias
        # (set via .AS("name")), the alias replaces the table name on the
        # left side of the dot — which is what makes self-joins and
        # mutually-aliased queries work without ambiguous column refs.
        # The params list is accepted but not used: column references
        # don't generate parameters.
        return f"{self._table._sql_name}.{self._field.column_name}"


# WeakValueDictionary so that holding a TableProxy keeps both the proxy and
# its target class alive, but losing all references to a model class (e.g.,
# one defined inside a test that has since finished) allows the proxy entry
# to be garbage-collected.  A plain dict would pin every model class ever
# wrapped for the lifetime of the process — a slow leak in long-lived
# applications that dynamically build dataclasses.
_proxy_cache: weakref.WeakValueDictionary[type, TableProxy[Any]] = (
    weakref.WeakValueDictionary()
)


# T parameterises TableProxy by the dataclass it wraps.  This is enough to
# thread the model type through `cygnet.Table(Account) -> TableProxy[Account]`
# and `cygnet.get(...) -> Account | None` without affecting runtime behaviour
# (ColumnProxy attributes are still stamped dynamically; static autocomplete
# on T.col is the deferred next step — see ISSUES.md item 5.4).
class TableProxy[T]:
    # Same __new__ / _initialised caching pattern as TableMeta.  The proxy
    # cache is separate from the meta cache so that their lifetimes are
    # independent (proxy → meta is a strong reference, but not vice versa).
    def __new__(cls, target_cls: type[T]) -> TableProxy[T]:
        if (cached := _proxy_cache.get(target_cls)) is not None:
            return cached
        instance = super().__new__(cls)
        _proxy_cache[target_cls] = instance
        return instance

    def __init__(self, target_cls: type[T]) -> None:
        # __new__ can return a cached instance, but Python still calls
        # __init__ on it.  The _initialised flag short-circuits re-running
        # the expensive introspection + column-proxy stamping on every
        # cygnet.Table(Cls) call.  Without this guard, the ColumnProxy
        # attributes would be rebuilt (and replaced) on each call, breaking
        # identity comparisons elsewhere in the codebase.
        if hasattr(self, "_initialised"):
            return
        # Set the flag BEFORE building the proxy state.  TableMeta() can in
        # principle trigger further attribute access on `self` (e.g., during
        # an exception's __repr__), and we want the guard to short-circuit
        # those nested calls cleanly rather than half-initialise twice.
        self._initialised = True
        self._meta = TableMeta(target_cls)
        # Aliased proxies (.AS("name")) carry _alias; the cached "main"
        # proxy returned by Table(cls) does not.  None is the unaliased
        # default — _sql_name resolves it to the meta's table name.
        self._alias: str | None = None
        # Stamp a ColumnProxy onto the proxy for every field in the model.
        # This is what makes T.name, T.email, etc. resolve to proxies.
        # After this loop, attribute access is plain Python — no __getattr__
        # magic — which keeps IDE autocompletion and type-checking simple.
        for f in self._meta.fields:
            setattr(self, f.attr_name, ColumnProxy(self, f))

    @property
    def _sql_name(self) -> str:
        # The name that should appear before "." in column refs and after
        # "AS" in FROM/JOIN clauses.  Aliased proxies prefer their alias;
        # everything else falls back to the table name from TableMeta.
        return self._alias if self._alias else self._meta.table_name

    def AS(self, alias: str) -> TableProxy[T]:  # noqa: N802
        """Return an aliased view of this proxy: `T.AS("a")` renders as
        `tablename AS a` in FROM/JOIN, and column refs use `a.column`.

        Aliased proxies bypass the WeakValueDictionary cache — every
        .AS() call returns a fresh TableProxy whose lifetime is bounded
        by the caller's reference.  Unaliased proxies remain singletons
        per class, so identity-based comparisons elsewhere in Cygnet
        (b._table is X) still hold for the canonical proxy.

        Aliasing is for SELECT-side use only — self-joins and the rare
        cross-join cases where the same table appears twice in one
        query.  INSERT / UPDATE / DELETE on an aliased proxy is
        unsupported and rejected at builder time with a ValueError: PG
        doesn't allow ``AS`` in DML target position, AND the
        ColumnProxies stamped onto an aliased view emit the alias on
        the left of the dot, so any WHERE / SET RHS that referenced the
        aliased proxy would resolve to an undefined identifier.  Pass
        the unaliased ``Table(<model>)`` proxy for DML.
        """
        # Bypass cache via __new__'s super-call path.  Re-runs the same
        # initialisation flow that the cached instance went through, but
        # against a brand-new self that the cache never sees.
        # The TableMeta is shared with the canonical proxy — introspection
        # is keyed by class, not by alias, so an aliased view sees the same
        # FieldMeta list.  Only _alias and the freshly-stamped ColumnProxies
        # differ, and the new ColumnProxies back-reference `new` (not the
        # canonical proxy), which is what makes their render_sql emit the
        # alias instead of the table name.
        new = object.__new__(type(self))
        new._initialised = True
        new._meta = self._meta
        new._alias = alias
        for f in new._meta.fields:
            setattr(new, f.attr_name, ColumnProxy(new, f))
        return new
