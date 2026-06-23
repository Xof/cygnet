# test_meta_field_filtering.py — TableMeta.fields must contain only genuine
# dataclass columns.  get_type_hints surfaces ClassVar, InitVar, and the
# KW_ONLY sentinel, none of which are columns; if they leak into meta.fields
# the renderer emits phantom SELECT columns that don't exist in the table.
#
# No `from __future__ import annotations` here on purpose: these tests exercise
# the real, un-stringized annotation forms (string annotations change how
# dataclasses detects the KW_ONLY sentinel).
import dataclasses
from dataclasses import InitVar
from typing import Annotated, ClassVar

import cygnet
from cygnet.annotations import DBKey
from cygnet.meta import TableMeta
from tests.conftest import FakeDB


def test_classvar_and_initvar_excluded_from_fields():
    @dataclasses.dataclass
    class M:
        id: Annotated[int, DBKey]
        name: str
        version: ClassVar[int] = 1
        seed: InitVar[int] = 0

        def __post_init__(self, seed: int) -> None: ...

    assert [f.attr_name for f in TableMeta(M).fields] == ["id", "name"]


def test_kw_only_sentinel_excluded_from_fields():
    @dataclasses.dataclass
    class M:
        id: Annotated[int, DBKey]
        _: dataclasses.KW_ONLY
        name: str

    assert [f.attr_name for f in TableMeta(M).fields] == ["id", "name"]


def test_initvar_model_uses_kwargs_builder():
    # Cross-feature lock: InitVar is filtered out of fields (not a column) but
    # stays in the constructor signature, so the positional gate sees a length
    # mismatch and falls back to kwargs — which still hydrates correctly (the
    # InitVar default is supplied to __post_init__).
    @dataclasses.dataclass
    class M:
        id: Annotated[int, DBKey]
        name: str
        seed: InitVar[int] = 0

        def __post_init__(self, seed: int) -> None: ...

    meta = cygnet.Table(M)._meta
    assert meta.row_builder.__name__ == "_build_kwargs"
    assert meta.row_builder((1, "Ann")) == M(1, "Ann")


def test_classvar_not_emitted_in_select_sql():
    @dataclasses.dataclass
    class M:
        id: Annotated[int, DBKey]
        name: str
        version: ClassVar[int] = 1

    sql, _ = cygnet.SELECT(FakeDB()).FROM(cygnet.Table(M)).sql()
    assert "version" not in sql
    assert "id" in sql and "name" in sql
