# meta.py — Introspects a dataclass into a TableMeta / FieldMeta description.
#
# This is the bridge between Python type hints and Cygnet's SQL generation.
# Every dataclass that Cygnet touches is introspected exactly once; the result
# is cached in a WeakValueDictionary keyed by the class object.  If no strong
# reference to the TableMeta survives, the entry is reclaimed — but in
# practice, TableProxy (proxy.py) holds a strong reference for the lifetime
# of the proxy, so the cache entry lives as long as the proxy does.

from __future__ import annotations

import dataclasses
import types
import typing
import weakref
from typing import Annotated, Union, get_args, get_origin, get_type_hints

from .annotations import DBKey, _Column, _ForeignKey, _PrimaryKey


def _unwrap_optional(t: type) -> type:
    """Unwrap Optional[X] / X | None → X, leaving everything else unchanged.

    Nullable foreign keys (Annotated[int | None, ForeignKey(Parent)]) are
    common in SQL. The FK type check needs to compare the base type against
    the target PK's type, ignoring the None alternative.
    """
    # Both Union spellings must be handled: typing.Optional[X] / typing.Union[…]
    # produce typing.Union as the origin, while X | None (PEP 604, 3.10+)
    # produces types.UnionType.  Checking both covers user style preference.
    # Wider unions (len(non_none) != 1) are left unchanged — Cygnet has no
    # meaningful FK type to compare them against, and the caller will surface
    # the resulting mismatch as a type-error message.
    origin = typing.get_origin(t)
    if origin is Union or origin is types.UnionType:
        non_none: list[type] = [a for a in typing.get_args(t) if a is not type(None)]
        if len(non_none) == 1:
            return non_none[0]
    return t


@dataclasses.dataclass
class FieldMeta:
    """One field's worth of introspection results.

    attr_name and column_name may differ when Column("...") overrides the
    default.  primary_key is None for non-PK fields.
    """

    # Invariant: attr_name is the Python identifier used on the dataclass;
    # column_name is what appears in SQL.  Column("x") overrides column_name
    # without touching attr_name — executor.py uses attr_name for getattr()
    # and column_name for rendering.  Keeping them separate is what allows
    # Python-style identifiers to coexist with SQL reserved-word column names.
    attr_name: str
    column_name: str
    python_type: type
    primary_key: _PrimaryKey | None
    foreign_key: _ForeignKey | None = None


# WeakValueDictionary so that introspection results for ephemeral or
# test-only models can be garbage-collected when no proxy holds them.
# Tests in particular define throwaway dataclasses inside test functions;
# a plain dict would accumulate one TableMeta per test and leak memory.
# Because proxy.TableProxy holds a strong reference to its TableMeta,
# the cache entry survives as long as the proxy does — which in practice
# is "as long as the user's model class is alive".
_cache: weakref.WeakValueDictionary[type, TableMeta] = weakref.WeakValueDictionary()


class TableMeta:
    # __new__ + _initialised guard implements a manual singleton-per-class
    # pattern.  We can't use __init_subclass__ or a metaclass because
    # TableMeta isn't subclassed — it's instantiated once per target dataclass.
    # The _initialised flag prevents __init__ from re-running on cache hits,
    # since __new__ returns an already-initialised object but Python still
    # calls __init__ on it.
    def __new__(cls, target_cls: type) -> TableMeta:
        if (cached := _cache.get(target_cls)) is not None:
            return cached
        instance = super().__new__(cls)
        _cache[target_cls] = instance
        return instance

    def __init__(self, target_cls: type) -> None:
        # Cache-hit short-circuit: __new__ may have returned an
        # already-initialised instance; re-running _introspect would
        # double-populate self.fields and corrupt the cached state.
        if hasattr(self, "_initialised"):
            return

        self.cls = target_cls
        # Default table name: lowercase class name + "s".
        # @cygnet.table("override") sets __cygnet_table__ to bypass this.
        self.table_name: str = (
            getattr(target_cls, "__cygnet_table__", None)
            or target_cls.__name__.lower() + "s"
        )
        self.fields: list[FieldMeta] = []
        self.pk: FieldMeta | None = None
        self._introspect()
        # _initialised is set ONLY after a successful _introspect so that a
        # class which fails introspection (no PK, duplicate PK, etc.) doesn't
        # leave a "fully initialised" empty TableMeta in the cache that
        # subsequent lookups would treat as valid.
        self._initialised = True

    def _introspect(self) -> None:
        if not dataclasses.is_dataclass(self.cls):
            raise TypeError(
                f"{self.cls.__name__} is not a dataclass — "
                f"CYGNET requires dataclasses as model objects"
            )

        # include_extras=True is essential: without it, get_type_hints strips
        # Annotated wrappers and we lose our DBKey/AppKey/Column metadata.
        # Iteration order of the returned dict follows the dataclass field
        # declaration order; self.fields preserves that order, and executor.py
        # relies on it for positional row-to-object mapping (see _row_to_obj
        # and _render_select's column emission in fields-order).
        hints = get_type_hints(self.cls, include_extras=True)
        for attr, hint in hints.items():
            pk_meta: _PrimaryKey | None = None
            fk_meta: _ForeignKey | None = None
            col_name = attr
            py_type = hint

            if get_origin(hint) is Annotated:
                args = get_args(hint)
                # args[0] is the underlying Python type; args[1:] are the
                # Annotated metadata objects (DBKey/AppKey/_Column/_ForeignKey).
                py_type = args[0]
                # Scan all Annotated args — order doesn't matter, and users
                # can combine PK + Column + ForeignKey in any position.
                for a in args[1:]:
                    if isinstance(a, _PrimaryKey):
                        pk_meta = a
                    elif isinstance(a, _ForeignKey):
                        if fk_meta is not None:
                            raise TypeError(
                                f"{self.cls.__name__}.{attr}: field has "
                                f"multiple ForeignKey annotations"
                            )
                        fk_meta = a
                    elif isinstance(a, _Column) and a.name:
                        col_name = a.name

            if pk_meta is not None:
                if self.pk is not None:
                    raise TypeError(
                        f"{self.cls.__name__} has more than one primary key annotation"
                    )
                # frozen + DBKey is a hard conflict: after INSERT RETURNING,
                # executor.py does setattr(obj, pk_attr, value), which raises
                # on frozen dataclasses.  We catch this early rather than
                # surfacing a confusing FrozenInstanceError at insert time.
                # AppKey + frozen is fine because the app supplies the PK
                # before the object is created.
                # `pk_meta == DBKey` is value-equality on the frozen
                # _PrimaryKey dataclass — both sides have assigned_by="db",
                # so this succeeds even if a user (or future code) ever
                # constructs a fresh _PrimaryKey("db") instead of reusing
                # the module-level singleton.
                if (
                    pk_meta == DBKey
                    and getattr(self.cls, "__dataclass_params__", None)
                    and self.cls.__dataclass_params__.frozen  # type: ignore[attr-defined]
                ):
                    raise TypeError(
                        f"{self.cls.__name__}: DBKey fields are incompatible "
                        f"with frozen=True — CYGNET cannot populate the key "
                        f"after INSERT. Use AppKey or remove frozen=True."
                    )

            # FK validation: catch misconfigurations at introspection time
            # rather than letting them surface as confusing SQL errors later.
            if fk_meta is not None:
                if pk_meta is not None:
                    raise TypeError(
                        f"{self.cls.__name__}.{attr}: a field cannot be both "
                        f"a primary key and a foreign key"
                    )
                # Introspect the target to validate it's a valid FK target.
                # This triggers the target's own introspection if not cached,
                # which will raise if the target isn't a dataclass.
                # Note: this recursion terminates because TableMeta.__new__
                # returns the cached instance on a second entry for the same
                # class.  Mutually-recursive models work as long as at least
                # one side is introspected first without a back-reference cycle
                # in the Annotated metadata itself (ForeignKey targets are
                # already-defined classes, so Python import order provides
                # that guarantee).
                target_meta = TableMeta(fk_meta.target)
                if target_meta.pk is None:
                    raise TypeError(
                        f"{self.cls.__name__}.{attr}: foreign key target "
                        f"{fk_meta.target.__name__} has no primary key"
                    )
                # Unwrap Optional for nullable FKs: int | None should
                # match an int PK. The None case is handled at query time.
                base_type = _unwrap_optional(py_type)
                if base_type != target_meta.pk.python_type:
                    raise TypeError(
                        f"{self.cls.__name__}.{attr}: foreign key type mismatch — "
                        f"{base_type.__name__} != {target_meta.pk.python_type.__name__}"
                    )

            fm = FieldMeta(attr, col_name, py_type, pk_meta, fk_meta)
            # self.fields mirrors declaration order — do not sort or reorder.
            # executor._row_to_obj uses zip(meta.fields, row), so any reorder
            # here would silently corrupt row-to-object mapping.
            self.fields.append(fm)
            if pk_meta is not None:
                self.pk = fm

        # Enforce "exactly one PK" at introspection time, matching the
        # documented invariant.  Without this, models with zero PKs would
        # only fail at the call site (save(), get(), upsert) — and most
        # operations downstream assume meta.pk is not None, so deferring
        # the check spreads guards across the codebase for no benefit.
        if self.pk is None:
            raise TypeError(
                f"{self.cls.__name__} has no primary key — "
                f"every CYGNET model must have exactly one DBKey or AppKey field"
            )

    @property
    def foreign_keys(self) -> list[FieldMeta]:
        """Fields that are foreign keys."""
        # Recomputed on each access rather than cached.  FK lookups are
        # infrequent (FOLLOW / LEFT_FOLLOW only) and self.fields is short,
        # so the filter is cheap; avoiding a cache keeps the invariant
        # "self.fields is the single source of truth" intact.
        return [f for f in self.fields if f.foreign_key is not None]
