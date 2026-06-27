"""profile_hotspots.py — deterministic + wall-clock hotspot profiler for Cygnet.

Companion to the pytest-benchmark suite (test_render / test_overhead /
test_hydration_micro).  Those tools answer "did this op get slower?"; this one
answers "*which functions* own the time, and how many times are they called?".

Design choices that make the numbers trustworthy:

  * FakeDB isolation — every workload runs against the in-memory FakeDB (same
    fixture the overhead bench uses), so no PG/network/asyncio-loop cost leaks
    into the profile.  What you see is pure-Cygnet Python.

  * Single-step coroutine drive — `await SELECT(db)...` is resolved by one
    `gen.send(None)` rather than an event loop.  FakeDB's async methods never
    suspend (no internal await), so the coroutine completes in one step; this
    keeps asyncio's selector/Task machinery out of the profile.  `_drive`
    asserts the single-step assumption so a future suspending adapter can't
    silently corrupt the measurement.

  * Grouped profiles — render, full-path (hydrate), and the row-builder micro
    have very different call shapes; blending them into one profile hides which
    path owns a shared callee.  Each group is profiled separately.

  * Two clocks — cProfile's per-call hook inflates call-heavy code, and an ORM
    is thousands of tiny calls.  So we ALSO report a clean `timeit` ns/op for
    each workload: timeit is the real per-op cost; cProfile attributes *where*
    that cost lives.  Trust timeit for "how fast", cProfile for "where".

Run:
    .venv/bin/python -m bench.profile_hotspots            # default iters
    .venv/bin/python -m bench.profile_hotspots 20000      # custom iters
    .venv/bin/python -m bench.profile_hotspots --group render
    .venv/bin/python -m bench.profile_hotspots --save     # write .prof files

.prof files (with --save) land in bench/.profiles/ for snakeviz / pstats.
"""

from __future__ import annotations

import argparse
import cProfile
import io
import os
import pstats
import sys
import timeit
from collections.abc import Callable
from pstats import SortKey
from typing import Any

import cygnet

# Reuse the canonical bench models/tables and the reference FakeDB adapter so
# this profiler measures exactly the work the pytest benches do — no drift.
from bench.conftest import Account, AccountTable, PostTable  # noqa: E402
from cygnet import functions as f
from tests.conftest import FakeDB  # noqa: E402

# Restrict ranked output to frames defined under the package whose name matches
# this — keeps stdlib/dataclass noise out of the "Cygnet hotspots" table while
# still letting the combined pstats file show everything.
CYGNET_PKG_DIR = os.path.dirname(cygnet.__file__)


def _drive(awaitable: Any) -> Any:
    """Resolve a Cygnet awaitable builder in one step, no event loop.

    Valid only because FakeDB's coroutines never suspend.  If that ever stops
    being true (an adapter that actually awaits I/O), the coroutine yields a
    non-None value here and we fail loudly rather than silently mis-measuring.
    """
    gen = awaitable.__await__()
    try:
        first = gen.send(None)
    except StopIteration as stop:
        return stop.value
    gen.close()
    raise RuntimeError(
        f"awaitable suspended (yielded {first!r}); FakeDB drive assumes no "
        "suspension — use an event loop for a truly async adapter"
    )


# ── Workload definitions ──────────────────────────────────────────────────
#
# Each workload is a zero-arg callable performing ONE operation.  Objects that
# are construction-invariant across iterations (the populated FakeDB, the bulk
# input lists) are built once at module load so the profile reflects the
# query/hydration path, not list-comprehension setup.  Per-op object creation
# that a real caller WOULD pay each time (the INSERT's source dataclass) is
# kept inside the op, matching test_overhead.py.

_DB_EMPTY = FakeDB()
_DB_1ROW = FakeDB(rows=[(1, "User 1", "user1@example.com")])
_DB_100 = FakeDB(rows=[(i, f"User {i}", f"user{i}@example.com") for i in range(1, 101)])
_DB_JOIN_50 = FakeDB(
    rows=[
        (i, f"User {i}", f"u{i}@example.com", 100 + i, i, f"Post {i}", "body")
        for i in range(1, 51)
    ]
)
_BULK_ACCOUNTS = [
    Account(id=None, name=f"User {i}", email=f"u{i}@example.com") for i in range(100)
]
_ROW_BUILDER = AccountTable._meta.row_builder
_HYDRATE_ROWS = [(i, f"User {i}", f"user{i}@example.com") for i in range(100)]


# -- render-only: builds the AST + renders (sql, params); no execute/hydrate --


def r_select_simple() -> tuple:
    return cygnet.SELECT(_DB_EMPTY).FROM(AccountTable).WHERE(AccountTable.id == 1).sql()


def r_select_compound() -> tuple:
    return (
        cygnet.SELECT(_DB_EMPTY)
        .FROM(AccountTable)
        .WHERE(AccountTable.id > 100)
        .WHERE(AccountTable.name == "Fred")
        .WHERE(AccountTable.email != "")
        .sql()
    )


def r_select_join() -> tuple:
    return (
        cygnet.SELECT(_DB_EMPTY)
        .FROM(AccountTable)
        .JOIN(PostTable, ON=AccountTable.id == PostTable.account_id)
        .WHERE(AccountTable.id > 50)
        .sql()
    )


def r_aggregate() -> tuple:
    return (
        cygnet.SELECT(_DB_EMPTY, AccountTable.name, f.count())
        .FROM(AccountTable)
        .GROUP_BY(AccountTable.name)
        .HAVING(f.count() > 1)
        .sql()
    )


def r_insert_one() -> tuple:
    acc = Account(id=None, name="Fred", email="fred@example.com")
    return cygnet.INSERT(_DB_EMPTY).INTO(AccountTable).VALUES(acc).sql()


def r_bulk_insert_100() -> tuple:
    return cygnet.INSERT(_DB_EMPTY).INTO(AccountTable).BULK_VALUES(_BULK_ACCOUNTS).sql()


def r_update() -> tuple:
    return (
        cygnet.UPDATE(_DB_EMPTY)
        .SET(AccountTable, name="Fred")
        .WHERE(AccountTable.id == 42)
        .sql()
    )


def r_delete() -> tuple:
    return (
        cygnet.DELETE(_DB_EMPTY).FROM(AccountTable).WHERE(AccountTable.id == 42).sql()
    )


# -- full path: render + execute(FakeDB) + row->object hydration --


def fp_select_1row() -> list:
    return _drive(cygnet.SELECT(_DB_1ROW).FROM(AccountTable))


def fp_select_100() -> list:
    return _drive(cygnet.SELECT(_DB_100).FROM(AccountTable))


def fp_select_columnar() -> list:
    # explicit columns => raw tuples, hydration branch skipped.  Subtract from
    # fp_select_100 to isolate per-row hydration cost.
    return _drive(
        cygnet.SELECT(_DB_100, AccountTable.id, AccountTable.name).FROM(AccountTable)
    )


def fp_join_50() -> list:
    return _drive(
        cygnet.SELECT(_DB_JOIN_50)
        .FROM(AccountTable)
        .JOIN(PostTable, ON=AccountTable.id == PostTable.account_id)
    )


def fp_insert_one() -> int:
    db = FakeDB(rows=[(1,)])
    acc = Account(id=None, name="Fred", email="fred@example.com")
    return _drive(cygnet.INSERT(db).INTO(AccountTable).VALUES(acc))


def fp_bulk_insert_100() -> list:
    db = FakeDB(rows=[(i,) for i in range(1, 101)])
    return _drive(cygnet.INSERT(db).INTO(AccountTable).BULK_VALUES(_BULK_ACCOUNTS))


def fp_update() -> Any:
    return _drive(
        cygnet.UPDATE(_DB_EMPTY)
        .SET(AccountTable, name="Fred")
        .WHERE(AccountTable.id == 42)
    )


# -- hydration micro: the positional row builder over a 100-row batch --


def hy_row_builder_100() -> list:
    return [_ROW_BUILDER(r) for r in _HYDRATE_ROWS]


GROUPS: dict[str, list[tuple[str, Callable[[], Any]]]] = {
    "render": [
        ("select_simple", r_select_simple),
        ("select_compound", r_select_compound),
        ("select_join", r_select_join),
        ("aggregate", r_aggregate),
        ("insert_one", r_insert_one),
        ("bulk_insert_100", r_bulk_insert_100),
        ("update", r_update),
        ("delete", r_delete),
    ],
    "fullpath": [
        ("select_1row", fp_select_1row),
        ("select_100", fp_select_100),
        ("select_columnar_100", fp_select_columnar),
        ("join_50", fp_join_50),
        ("insert_one", fp_insert_one),
        ("bulk_insert_100", fp_bulk_insert_100),
        ("update", fp_update),
    ],
    "hydrate": [
        ("row_builder_100", hy_row_builder_100),
    ],
}


# ── Measurement ────────────────────────────────────────────────────────────


def wallclock_ns(fn: Callable[[], Any], iters: int) -> float:
    """Clean (un-profiled) ns/op via timeit's best-of-5 minimum."""
    best = min(timeit.repeat(fn, number=iters, repeat=5)) / iters
    return best * 1e9


def profile_group(
    name: str, workloads: list[tuple[str, Callable[[], Any]]], iters: int
) -> tuple[cProfile.Profile, list[tuple[str, float]]]:
    """Warm up, time (timeit), then cProfile every workload in the group into
    one shared Profile.  Returns (profile, [(workload, ns/op), ...])."""
    timings: list[tuple[str, float]] = []
    pr = cProfile.Profile()
    for wl_name, fn in workloads:
        fn()  # warm up: trigger any lazy per-table caching off the hot path
        timings.append((wl_name, wallclock_ns(fn, iters)))
        pr.enable()
        for _ in range(iters):
            fn()
        pr.disable()
    return pr, timings


def print_timings(group: str, timings: list[tuple[str, float]]) -> None:
    print(f"\n## {group} — wall-clock (timeit, un-profiled, ns/op)")
    width = max(len(n) for n, _ in timings)
    for wl_name, ns in sorted(timings, key=lambda t: -t[1]):
        print(f"  {wl_name:<{width}}  {ns:>12,.0f} ns/op")


def print_hotspots(group: str, pr: cProfile.Profile, top: int = 18) -> None:
    """Two ranked tables from the group's profile: self-time (tottime) and
    cumulative-time, both restricted to frames defined in the cygnet package."""
    for sortkey, label in (
        (SortKey.TIME, "tottime (self)"),
        (SortKey.CUMULATIVE, "cumtime"),
    ):
        buf = io.StringIO()
        st = pstats.Stats(pr, stream=buf).strip_dirs().sort_stats(sortkey)
        # strip_dirs drops paths, so filter by the cygnet source basenames.
        st.print_stats(_cygnet_filename_regex(), top)
        print(f"\n## {group} — top {top} cygnet frames by {label}")
        print(_trim_pstats(buf.getvalue()))


_CYGNET_BASENAMES = {fn for fn in os.listdir(CYGNET_PKG_DIR) if fn.endswith(".py")}


def _cygnet_filename_regex() -> str:
    # pstats filters on "filename:lineno(func)" text; after strip_dirs the
    # filename is just the basename.  Match any cygnet module basename.
    return (
        r"^("
        + "|".join(b.replace(".", r"\.") for b in sorted(_CYGNET_BASENAMES))
        + r"):"
    )


def _trim_pstats(text: str) -> str:
    # Keep the ncalls/tottime/cumtime table; drop pstats' verbose preamble.
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if "ncalls" in line and "tottime" in line:
            return "\n".join(lines[i:])
    return text


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "iters", nargs="?", type=int, default=5000, help="iterations per workload"
    )
    ap.add_argument("--group", choices=[*GROUPS, "all"], default="all")
    ap.add_argument(
        "--save", action="store_true", help="write .prof files to bench/.profiles/"
    )
    ap.add_argument("--top", type=int, default=18)
    args = ap.parse_args()

    groups = GROUPS if args.group == "all" else {args.group: GROUPS[args.group]}
    py = sys.version.split()[0]
    print(f"Cygnet hotspot profile — {args.iters:,} iters/workload, py{py}")
    print("FakeDB isolation (no PG/network/asyncio); timeit=real cost, cProfile=where.")

    for name, workloads in groups.items():
        pr, timings = profile_group(name, workloads, args.iters)
        print_timings(name, timings)
        print_hotspots(name, pr, args.top)
        if args.save:
            os.makedirs("bench/.profiles", exist_ok=True)
            pr.dump_stats(f"bench/.profiles/{name}.prof")
            print(f"\n  saved bench/.profiles/{name}.prof")

    print("\nTip: snakeviz bench/.profiles/<group>.prof  for a flamegraph view.")


if __name__ == "__main__":
    main()
