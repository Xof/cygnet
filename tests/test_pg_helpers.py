# test_pg_helpers.py — Unit tests for the PG-native helper modules:
# cygnet.jsonb, cygnet.arrays, cygnet.fts.
#
# Each helper is a thin alias around cygnet.op or cygnet.fn, so the tests
# focus on (a) operator/function name fidelity, (b) parameter handling for
# value operands, and (c) correct interaction with the larger Predicate /
# WHERE / ORDER_BY machinery.

from __future__ import annotations

import dataclasses
from typing import Annotated

import cygnet
import cygnet.arrays as arr
import cygnet.fts as fts
import cygnet.jsonb as jb
from cygnet.annotations import DBKey
from tests.conftest import AccountTable, FakeDB


@dataclasses.dataclass
class Document:
    id: Annotated[int, DBKey]
    title: str
    body: str
    tags: list[str]
    payload: dict


DocumentTable = cygnet.Table(Document)


class TestJsonb:
    def test_get_renders_arrow(self):
        params: list = []
        sql = jb.get(DocumentTable.payload, "name").render_sql(params)
        assert sql == "documents.payload -> $1"
        assert params == ["name"]

    def test_get_text_renders_double_arrow(self):
        params: list = []
        sql = jb.get_text(DocumentTable.payload, "name").render_sql(params)
        assert sql == "documents.payload ->> $1"
        assert params == ["name"]

    def test_get_path(self):
        params: list = []
        sql = jb.get_path(DocumentTable.payload, ["a", "b"]).render_sql(params)
        assert sql == "documents.payload #> $1"
        assert params == [["a", "b"]]

    def test_contains_in_predicate(self):
        params: list = []
        sql = jb.contains(DocumentTable.payload, '{"x": 1}').render_sql(params)
        assert sql == "documents.payload @> $1"
        assert params == ['{"x": 1}']

    def test_has_key_chains_with_and(self):
        """JSONB helpers compose with & / | like any Predicate."""
        params: list = []
        pred = jb.has_key(DocumentTable.payload, "email") & (DocumentTable.id > 10)
        sql = pred.render_sql(params)
        assert sql == "(documents.payload ? $1) AND (documents.id > $2)"
        assert params == ["email", 10]

    def test_get_text_compares_to_value(self):
        """The most common JSONB usage: extract a field as text and compare."""
        params: list = []
        # (data ->> 'name') = 'Fred'  (PG: ->> binds tighter than =)
        pred = jb.get_text(DocumentTable.payload, "name") == "Fred"
        sql = pred.render_sql(params)
        assert sql == "documents.payload ->> $1 = $2"
        assert params == ["name", "Fred"]

    def test_path_match_at(self):
        params: list = []
        sql = jb.path_match(DocumentTable.payload, "$.x > 1").render_sql(params)
        assert sql == "documents.payload @@ $1"

    async def test_jsonb_in_full_query(self):
        """End-to-end through SelectBuilder + FakeDB."""
        db = FakeDB(rows=[])
        await (
            cygnet.SELECT(db)
            .FROM(DocumentTable)
            .WHERE(jb.contains(DocumentTable.payload, '{"active": true}'))
        )
        assert "WHERE (documents.payload @> $1)" in db.last_sql
        assert db.last_params == ['{"active": true}']


class TestArrays:
    def test_contains(self):
        params: list = []
        sql = arr.contains(DocumentTable.tags, ["python", "sql"]).render_sql(params)
        assert sql == "documents.tags @> $1"
        assert params == [["python", "sql"]]

    def test_overlaps(self):
        params: list = []
        sql = arr.overlaps(DocumentTable.tags, ["a"]).render_sql(params)
        assert sql == "documents.tags && $1"

    def test_any_paired_with_equality(self):
        """The canonical usage: `value = ANY(array_col)`."""
        params: list = []
        pred = AccountTable.id == arr.any(DocumentTable.tags)
        sql = pred.render_sql(params)
        assert sql == "accounts.id = ANY(documents.tags)"
        assert params == []

    def test_all_with_inequality(self):
        params: list = []
        pred = AccountTable.id > arr.all(DocumentTable.tags)
        sql = pred.render_sql(params)
        assert sql == "accounts.id > ALL(documents.tags)"

    def test_length_default_dim(self):
        params: list = []
        sql = arr.length(DocumentTable.tags).render_sql(params)
        assert sql == "array_length(documents.tags, $1)"
        assert params == [1]

    def test_length_explicit_dim(self):
        params: list = []
        sql = arr.length(DocumentTable.tags, dim=2).render_sql(params)
        assert sql == "array_length(documents.tags, $1)"
        assert params == [2]

    def test_cardinality(self):
        params: list = []
        sql = arr.cardinality(DocumentTable.tags).render_sql(params)
        assert sql == "cardinality(documents.tags)"

    async def test_arrays_in_where(self):
        db = FakeDB(rows=[])
        await (
            cygnet.SELECT(db)
            .FROM(DocumentTable)
            .WHERE(arr.contains(DocumentTable.tags, ["python"]))
            .WHERE(arr.length(DocumentTable.tags) > 0)
        )
        sql = db.last_sql
        assert "documents.tags @> $1" in sql
        assert "array_length(documents.tags, $2) > $3" in sql


class TestFts:
    def test_to_tsvector_default_config(self):
        params: list = []
        sql = fts.to_tsvector(DocumentTable.body).render_sql(params)
        assert sql == "to_tsvector($1, documents.body)"
        assert params == ["english"]

    def test_to_tsvector_custom_config(self):
        params: list = []
        fts.to_tsvector(DocumentTable.body, config="spanish").render_sql(params)
        assert params == ["spanish"]

    def test_matches_renders_at_at(self):
        params: list = []
        sql = fts.matches(
            fts.to_tsvector(DocumentTable.body),
            fts.web_query("fierce ORM"),
        ).render_sql(params)
        assert sql == (
            "to_tsvector($1, documents.body) @@ websearch_to_tsquery($2, $3)"
        )
        assert params == ["english", "english", "fierce ORM"]

    def test_rank_in_order_by(self):
        params: list = []
        # Mimic the executor's ORDER_BY rendering: append " DESC" because
        # rank() is a FunctionCall, not a Literal.
        sql = fts.rank(
            fts.to_tsvector(DocumentTable.body),
            fts.web_query("fierce"),
        ).render_sql(params)
        assert sql.startswith("ts_rank(to_tsvector(")
        assert "websearch_to_tsquery" in sql

    async def test_fts_in_full_query(self):
        db = FakeDB(rows=[])
        await (
            cygnet.SELECT(db)
            .FROM(DocumentTable)
            .WHERE(
                fts.matches(
                    fts.to_tsvector(DocumentTable.body),
                    fts.web_query("fierce ORM"),
                )
            )
            .ORDER_BY(
                fts.rank(
                    fts.to_tsvector(DocumentTable.body),
                    fts.web_query("fierce ORM"),
                ),
                DESC=True,
            )
        )
        sql = db.last_sql
        assert "@@" in sql
        assert "websearch_to_tsquery" in sql
        # ORDER BY should append DESC because ts_rank's FunctionCall doesn't
        # opt out of direction suffixing.
        assert sql.rstrip().endswith("DESC")
