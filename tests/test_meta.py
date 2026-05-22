# test_meta.py — Tests for dataclass introspection (TableMeta / FieldMeta).
#
# Covers field discovery, table naming (default and @cygnet.table override),
# column renaming (Column("...")), PK detection (DBKey/AppKey), error cases
# (non-dataclass, duplicate PK, frozen + DBKey), inheritance, and caching.

from __future__ import annotations

import dataclasses
from typing import Annotated

import pytest

import cygnet
from cygnet.annotations import AppKey, Column, DBKey
from cygnet.meta import TableMeta
from tests.conftest import Account, Event, LogEntry, TaggedAccount


class TestTableMeta:
    def test_basic_fields(self):
        meta = TableMeta(Account)
        assert [f.attr_name for f in meta.fields] == ["id", "name", "email"]

    def test_default_table_name(self):
        assert TableMeta(Account).table_name == "accounts"

    def test_explicit_table_name(self):
        assert TableMeta(LogEntry).table_name == "log_entries"

    def test_column_name_override(self):
        meta = TableMeta(TaggedAccount)
        tag_field = next(f for f in meta.fields if f.attr_name == "tag")
        assert tag_field.column_name == "tag_name"

    def test_column_name_default(self):
        meta = TableMeta(Account)
        name_field = next(f for f in meta.fields if f.attr_name == "name")
        assert name_field.column_name == "name"

    def test_dbkey_recognised(self):
        meta = TableMeta(Account)
        assert meta.pk is not None
        assert meta.pk.attr_name == "id"
        assert meta.pk.primary_key == DBKey

    def test_appkey_recognised(self):
        meta = TableMeta(Event)
        assert meta.pk is not None
        assert meta.pk.primary_key == AppKey

    def test_no_pk_raises_at_introspection(self):
        """Models without a PK are rejected at introspection time, not
        deferred to the first save()/get() call."""

        @dataclasses.dataclass
        class Keyless:
            name: str

        with pytest.raises(TypeError, match="no primary key"):
            TableMeta(Keyless)

    def test_failed_introspection_evicts_cache(self):
        """S26: a class that raises during _introspect must not leave a
        half-built TableMeta entry in the cache.  Otherwise a subsequent
        TableMeta(cls) lookup short-circuits through __new__'s cache
        hit, runs __init__ again (no _initialised guard), and re-fails
        with the same error — but anybody poking the cache directly
        would see a "fully-constructed" empty-fields TableMeta that
        wasn't fully constructed at all.
        """
        from cygnet.meta import _cache

        @dataclasses.dataclass
        class BrokenModel:
            name: str  # No PK — will raise.

        with pytest.raises(TypeError, match="no primary key"):
            TableMeta(BrokenModel)
        # The class must NOT be present in the cache after the failed
        # introspection — eviction-on-failure preserves the
        # "every cached entry is fully initialised" invariant.
        assert BrokenModel not in _cache

    def test_non_dataclass_raises(self):
        class NotADataclass:
            name: str

        with pytest.raises(TypeError, match="not a dataclass"):
            TableMeta(NotADataclass)

    def test_duplicate_pk_raises(self):
        with pytest.raises(TypeError, match="more than one primary key"):

            @dataclasses.dataclass
            class TwoPKs:
                id: Annotated[int, DBKey]
                alt_id: Annotated[int, DBKey]

            TableMeta(TwoPKs)

    def test_frozen_dbkey_raises(self):
        with pytest.raises(TypeError, match="frozen=True"):

            @dataclasses.dataclass(frozen=True)
            class FrozenBad:
                id: Annotated[int, DBKey]
                name: str

            TableMeta(FrozenBad)

    def test_frozen_appkey_ok(self):
        @dataclasses.dataclass(frozen=True)
        class FrozenOk:
            id: Annotated[str, AppKey]
            name: str

        meta = TableMeta(FrozenOk)
        assert meta.pk is not None
        assert meta.pk.primary_key == AppKey

    def test_cache_returns_same_instance(self):
        assert TableMeta(Account) is TableMeta(Account)

    def test_cache_independent_per_class(self):
        assert TableMeta(Account) is not TableMeta(Event)

    def test_dbkey_with_column_rename(self):
        @dataclasses.dataclass
        class RenamedPK:
            account_id: Annotated[int, DBKey, Column("id")]
            name: str

        meta = TableMeta(RenamedPK)
        assert meta.pk is not None
        assert meta.pk.attr_name == "account_id"
        assert meta.pk.column_name == "id"

    def test_inherited_dataclass(self):
        @dataclasses.dataclass
        class Base:
            id: Annotated[int, DBKey]
            name: str

        @dataclasses.dataclass
        class Child(Base):
            email: str

        meta = TableMeta(Child)
        assert len(meta.fields) == 3
        assert [f.attr_name for f in meta.fields] == ["id", "name", "email"]
        assert meta.pk is not None

    def test_table_proxy_cache_identity(self):
        assert cygnet.Table(Account) is cygnet.Table(Account)


# Module-level models for FK tests — must be at module scope so that
# get_type_hints can resolve forward references created by
# `from __future__ import annotations`.


@dataclasses.dataclass
class _FKParent:
    id: Annotated[int, DBKey]
    name: str


@dataclasses.dataclass
class _FKChild:
    id: Annotated[int, DBKey]
    parent_id: Annotated[int, cygnet.ForeignKey(_FKParent)]


class _NotADataclass:
    pass


@dataclasses.dataclass
class _NoPK:
    name: str


class TestForeignKey:
    def test_fk_recognised(self):
        meta = TableMeta(_FKChild)
        fk_field = next(f for f in meta.fields if f.attr_name == "parent_id")
        assert fk_field.foreign_key is not None
        assert fk_field.foreign_key.target is _FKParent

    def test_duplicate_fk_raises(self):
        with pytest.raises(TypeError, match="multiple ForeignKey"):

            @dataclasses.dataclass
            class DuplicateFKChild:
                id: Annotated[int, DBKey]
                parent_id: Annotated[
                    int, cygnet.ForeignKey(_FKParent), cygnet.ForeignKey(_FKParent)
                ]

            TableMeta(DuplicateFKChild)

    def test_non_fk_field_has_none(self):
        meta = TableMeta(Account)
        name_field = next(f for f in meta.fields if f.attr_name == "name")
        assert name_field.foreign_key is None

    def test_fk_target_not_dataclass_raises(self):
        with pytest.raises(TypeError, match="not a dataclass"):

            @dataclasses.dataclass
            class BadChild:
                id: Annotated[int, DBKey]
                parent_id: Annotated[int, cygnet.ForeignKey(_NotADataclass)]

            TableMeta(BadChild)

    def test_fk_target_no_pk_raises(self):
        with pytest.raises(TypeError, match="no primary key"):

            @dataclasses.dataclass
            class BadChild:
                id: Annotated[int, DBKey]
                parent_id: Annotated[int, cygnet.ForeignKey(_NoPK)]

            TableMeta(BadChild)

    def test_fk_and_pk_on_same_field_raises(self):
        with pytest.raises(TypeError, match="cannot be both"):

            @dataclasses.dataclass
            class BadChild:
                id: Annotated[int, DBKey, cygnet.ForeignKey(_FKParent)]

            TableMeta(BadChild)

    def test_fk_type_mismatch_raises(self):
        with pytest.raises(TypeError, match="type mismatch"):

            @dataclasses.dataclass
            class BadChild:
                id: Annotated[int, DBKey]
                parent_id: Annotated[str, cygnet.ForeignKey(_FKParent)]

            TableMeta(BadChild)

    def test_nullable_fk_accepted(self):
        """int | None FK should match an int PK — nullable FKs are common."""

        @dataclasses.dataclass
        class NullableChild:
            id: Annotated[int, DBKey]
            parent_id: Annotated[int | None, cygnet.ForeignKey(_FKParent)]

        meta = TableMeta(NullableChild)
        fk_field = next(f for f in meta.fields if f.attr_name == "parent_id")
        assert fk_field.foreign_key is not None

    def test_foreign_keys_property(self):
        meta = TableMeta(_FKChild)
        fks = meta.foreign_keys
        assert len(fks) == 1
        assert fks[0].attr_name == "parent_id"

    def test_foreign_keys_empty_when_none(self):
        meta = TableMeta(Account)
        assert meta.foreign_keys == []
