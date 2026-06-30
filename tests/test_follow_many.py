# test_follow_many.py — Unit tests for cygnet.follow_many, the batched FK
# navigation helper that collapses the N+1 `[follow(o) for o in objs]` pattern
# into a single `WHERE pk = ANY($1)` round-trip and re-associates the results
# back to the input objects (input order, None for a NULL FK or a missing row).

import dataclasses
from typing import Annotated

import pytest

import cygnet
from cygnet.annotations import DBKey
from tests.conftest import FakeDB


@dataclasses.dataclass
class Customer:
    id: Annotated[int, DBKey]
    name: str


@dataclasses.dataclass
class Order:
    id: Annotated[int, DBKey]
    customer_id: Annotated[int, cygnet.ForeignKey(Customer)]
    amount: float


CustomerTable = cygnet.Table(Customer)
OrderTable = cygnet.Table(Order)


def _orders(*customer_ids: int | None) -> list[Order]:
    return [Order(id=i, customer_id=c, amount=1.0) for i, c in enumerate(customer_ids)]


class TestFollowMany:
    async def test_aligned_to_input_order_one_query(self):
        """Returns the FK target per input object, in input order, in ONE query."""
        # FakeDB returns these regardless of the ANY param; order is arbitrary.
        db = FakeDB(rows=[(20, "Bob"), (10, "Alice")])
        orders = _orders(10, 20, 10)
        result = await cygnet.follow_many(db, orders, OrderTable.customer_id)
        assert [c.name for c in result] == ["Alice", "Bob", "Alice"]
        assert len(db.calls) == 1, "must be a single round-trip"
        assert "= ANY($1)" in db.last_sql

    async def test_none_fk_maps_to_none_and_is_not_queried(self):
        db = FakeDB(rows=[(10, "Alice")])
        orders = _orders(10, None, 10)
        result = await cygnet.follow_many(db, orders, OrderTable.customer_id)
        assert result[1] is None
        assert [c and c.name for c in result] == ["Alice", None, "Alice"]
        # None FK must not appear in the queried array.
        assert db.last_params == [[10]]

    async def test_missing_target_maps_to_none(self):
        """An FK value with no matching row maps to None (not an error)."""
        db = FakeDB(rows=[(10, "Alice")])  # 99 has no row
        result = await cygnet.follow_many(db, _orders(10, 99), OrderTable.customer_id)
        assert result[0].name == "Alice"
        assert result[1] is None

    async def test_empty_list_returns_empty_no_query(self):
        db = FakeDB(rows=[(10, "Alice")])
        result = await cygnet.follow_many(db, [], OrderTable.customer_id)
        assert result == []
        assert db.calls == [], "empty input must not hit the DB"

    async def test_all_none_fks_no_query(self):
        db = FakeDB(rows=[(10, "Alice")])
        result = await cygnet.follow_many(
            db, _orders(None, None), OrderTable.customer_id
        )
        assert result == [None, None]
        assert db.calls == [], "all-None FKs must not hit the DB"

    async def test_duplicate_fks_share_one_instance(self):
        """Distinct FK values are fetched once; objects sharing a value share
        the returned instance (a batching artifact — Cygnet has no identity map)."""
        db = FakeDB(rows=[(10, "Alice")])
        result = await cygnet.follow_many(db, _orders(10, 10), OrderTable.customer_id)
        assert result[0] is result[1]
        # Deduped: the ANY array carries the value once.
        assert db.last_params == [[10]]

    async def test_non_fk_column_raises(self):
        db = FakeDB()
        with pytest.raises(ValueError, match="not a foreign key"):
            await cygnet.follow_many(db, _orders(10), OrderTable.amount)

    async def test_non_column_proxy_raises(self):
        db = FakeDB()
        with pytest.raises(ValueError, match="not a column proxy"):
            await cygnet.follow_many(db, _orders(10), "customer_id")

    async def test_wrong_object_type_raises(self):
        db = FakeDB(rows=[(10, "Alice")])
        bad = [Customer(id=1, name="X")]  # not an Order
        with pytest.raises(TypeError, match="Expected Order"):
            await cygnet.follow_many(db, bad, OrderTable.customer_id)

    async def test_wrong_type_error_precedes_fk_error(self):
        """When both the objects are the wrong type AND the column is not an FK,
        the type error wins — matching single-row follow()'s precedence."""
        db = FakeDB()
        bad = [Customer(id=1, name="X")]  # wrong model
        with pytest.raises(TypeError, match="Expected Order"):
            await cygnet.follow_many(db, bad, OrderTable.amount)  # amount: not an FK

    async def test_targets_the_fk_table_by_pk(self):
        """The single query selects the FK's target table, filtered by its PK."""
        db = FakeDB(rows=[(10, "Alice")])
        await cygnet.follow_many(db, _orders(10), OrderTable.customer_id)
        assert "FROM customers" in db.last_sql
        assert "customers.id = ANY($1)" in db.last_sql
