# test_hydration_micro.py — Trend artifact for the row builder over a 100-row
# batch.  Report-only: per project CI policy, benchmark steps never gate the
# pipeline; this exists so a regression in the chosen construction strategy is
# visible in trend tracking.
import pytest

from .conftest import Account, AccountTable

pytestmark = pytest.mark.bench


def test_row_builder_100_rows(benchmark):
    build = AccountTable._meta.row_builder
    rows = [(i, f"User {i}", f"user{i}@example.com") for i in range(100)]

    def op():
        return [build(r) for r in rows]

    result = benchmark(op)
    # Sanity: the positional builder must be selected for a plain dataclass.
    assert build.__name__ == "_build_positional"
    assert result[0] == Account(id=0, name="User 0", email="user0@example.com")
