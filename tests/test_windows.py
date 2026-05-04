# test_windows.py — Tests for window functions: FunctionCall.OVER(...) and
# the curated window-function aliases in cygnet.functions.

from __future__ import annotations

import dataclasses
from typing import Annotated

import pytest  # noqa: F401  (used by future xfail/raises tests)

import cygnet
import cygnet.functions as f
from cygnet.annotations import DBKey


@dataclasses.dataclass
class Employee:
    id: Annotated[int, DBKey]
    name: str
    dept: str
    salary: int


EmployeeTable = cygnet.Table(Employee)


class TestWindowSQL:
    def test_row_number_over_partition_and_order(self):
        params: list = []
        expr = f.row_number().OVER(
            partition_by=[EmployeeTable.dept],
            order_by=[(EmployeeTable.salary, "DESC")],
        )
        sql = expr.render_sql(params)
        assert sql == (
            "row_number() OVER (PARTITION BY employees.dept "
            "ORDER BY employees.salary DESC)"
        )
        assert params == []

    def test_order_by_default_is_asc(self):
        """Bare order_by entries default to ASC."""
        params: list = []
        expr = f.rank().OVER(order_by=[EmployeeTable.salary])
        sql = expr.render_sql(params)
        assert sql == "rank() OVER (ORDER BY employees.salary ASC)"

    def test_partition_only(self):
        params: list = []
        expr = f.count().OVER(partition_by=[EmployeeTable.dept])
        sql = expr.render_sql(params)
        assert sql == "count(*) OVER (PARTITION BY employees.dept)"

    def test_empty_over(self):
        """`func() OVER ()` — running window across all rows."""
        params: list = []
        expr = f.sum(EmployeeTable.salary).OVER()
        sql = expr.render_sql(params)
        assert sql == "sum(employees.salary) OVER ()"

    def test_lag_with_args(self):
        """LAG takes its column and offset; both are part of the function call,
        not the OVER clause."""
        params: list = []
        expr = f.lag(EmployeeTable.salary, 1).OVER(
            partition_by=[EmployeeTable.dept],
            order_by=[EmployeeTable.id],
        )
        sql = expr.render_sql(params)
        assert sql == (
            "lag(employees.salary, $1) OVER (PARTITION BY employees.dept "
            "ORDER BY employees.id ASC)"
        )
        assert params == [1]

    def test_frame_string(self):
        """A raw frame string is appended verbatim after PARTITION/ORDER."""
        params: list = []
        expr = f.sum(EmployeeTable.salary).OVER(
            order_by=[EmployeeTable.id],
            frame="ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW",
        )
        sql = expr.render_sql(params)
        expected_tail = (
            "ORDER BY employees.id ASC "
            "ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)"
        )
        assert sql.endswith(expected_tail)

    def test_window_in_select_list(self):
        """A WindowExpression slots into SELECT's columns alongside ColumnProxies."""
        from tests.conftest import FakeDB

        db = FakeDB(rows=[])

        async def _run():
            await cygnet.SELECT(
                db,
                EmployeeTable.name,
                f.row_number().OVER(
                    partition_by=[EmployeeTable.dept],
                    order_by=[(EmployeeTable.salary, "DESC")],
                ),
            ).FROM(EmployeeTable)

        import asyncio

        asyncio.run(_run())
        assert (
            "row_number() OVER (PARTITION BY employees.dept "
            "ORDER BY employees.salary DESC)" in db.last_sql
        )

    def test_window_compares_for_filtering(self):
        """A window expression supports comparisons → Predicate, so it can
        sit on the LHS of WHERE or HAVING.  PG won't let you reference a
        window in the same query's WHERE, but the AST composition still
        needs to work for nested subqueries."""
        params: list = []
        win = f.rank().OVER(order_by=[EmployeeTable.salary])
        pred = win <= 3
        sql = pred.render_sql(params)
        assert sql == "rank() OVER (ORDER BY employees.salary ASC) <= $1"
        assert params == [3]

    def test_aggregate_used_as_window(self):
        """Plain aggregates work as windows — sum() over partition."""
        params: list = []
        expr = f.sum(EmployeeTable.salary).OVER(
            partition_by=[EmployeeTable.dept],
        )
        sql = expr.render_sql(params)
        assert sql == "sum(employees.salary) OVER (PARTITION BY employees.dept)"

    def test_multiple_order_columns_with_mixed_directions(self):
        params: list = []
        expr = f.dense_rank().OVER(
            partition_by=[EmployeeTable.dept],
            order_by=[
                (EmployeeTable.salary, "DESC"),
                EmployeeTable.id,  # default ASC
            ],
        )
        sql = expr.render_sql(params)
        assert "ORDER BY employees.salary DESC, employees.id ASC" in sql
