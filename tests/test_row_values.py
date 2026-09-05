# test_row_values.py — Tests for row-value constructors (cygnet.row) and the
# IN-list predicate (cygnet.in_).
#
# Row values are the SQL `(a, b)` construct: a tuple of expressions usable
# wherever a scalar expression is, and comparable against another row with
# `=`, `<`, `>`, etc.  They exist so a composite key can be compared or
# batched in one predicate.  These tests exercise the rendering and the
# fail-loud arity/emptiness validation directly — no builders, no executor —
# plus one end-to-end $N-ordering check through a real SELECT render.

from __future__ import annotations

import pytest

import cygnet
from tests.conftest import AccountTable, FakeDB, LogTable


class TestRowConstruction:
    def test_renders_parenthesised_column_list(self):
        params: list = []
        sql = cygnet.row(AccountTable.id, AccountTable.name).render_sql(params)
        assert sql == "(accounts.id, accounts.name)"
        assert params == []

    def test_single_element_row_is_allowed(self):
        # Degenerate but useful: code that builds a row from a key of
        # unknown length shouldn't have to special-case length 1.
        params: list = []
        sql = cygnet.row(AccountTable.id).render_sql(params)
        assert sql == "(accounts.id)"
        assert params == []

    def test_plain_values_become_parameters(self):
        params: list = []
        sql = cygnet.row(AccountTable.id, "Fred").render_sql(params)
        assert sql == "(accounts.id, $1)"
        assert params == ["Fred"]

    def test_empty_row_raises(self):
        with pytest.raises(TypeError, match="at least one"):
            cygnet.row()

    def test_row_is_unhashable(self):
        # __eq__ returns a Predicate, so hashing would be inconsistent with
        # equality — same contract as ColumnProxy / FunctionCall.
        with pytest.raises(TypeError):
            hash(cygnet.row(AccountTable.id, AccountTable.name))


class TestRowComparison:
    def test_equality_against_tuple(self):
        params: list = []
        pred = cygnet.row(AccountTable.id, AccountTable.name) == (1, "Fred")
        sql = pred.render_sql(params)
        assert sql == "(accounts.id, accounts.name) = ($1, $2)"
        assert params == [1, "Fred"]

    def test_equality_against_list(self):
        params: list = []
        pred = cygnet.row(AccountTable.id, AccountTable.name) == [1, "Fred"]
        sql = pred.render_sql(params)
        assert sql == "(accounts.id, accounts.name) = ($1, $2)"
        assert params == [1, "Fred"]

    def test_greater_than_for_keyset_pagination(self):
        params: list = []
        pred = cygnet.row(AccountTable.id, AccountTable.name) > (10, "x")
        sql = pred.render_sql(params)
        assert sql == "(accounts.id, accounts.name) > ($1, $2)"
        assert params == [10, "x"]

    @pytest.mark.parametrize("op", ["!=", "<", "<=", ">="])
    def test_remaining_comparison_operators(self, op):
        ops = {
            "!=": lambda r, v: r != v,
            "<": lambda r, v: r < v,
            "<=": lambda r, v: r <= v,
            ">=": lambda r, v: r >= v,
        }
        params: list = []
        pred = ops[op](cygnet.row(AccountTable.id, AccountTable.name), (1, "Fred"))
        sql = pred.render_sql(params)
        assert sql == f"(accounts.id, accounts.name) {op} ($1, $2)"
        assert params == [1, "Fred"]

    def test_row_to_row_comparison_binds_no_parameters(self):
        params: list = []
        pred = cygnet.row(AccountTable.id, AccountTable.name) == cygnet.row(
            LogTable.id, LogTable.message
        )
        sql = pred.render_sql(params)
        assert (
            sql
            == "(accounts.id, accounts.name) = (log_entries.id, log_entries.message)"
        )
        assert params == []

    def test_arity_mismatch_against_tuple_raises(self):
        with pytest.raises(ValueError, match="2 element"):
            cygnet.row(AccountTable.id, AccountTable.name) == (1,)

    def test_arity_mismatch_against_row_raises(self):
        with pytest.raises(ValueError, match="2 element"):
            cygnet.row(AccountTable.id, AccountTable.name) == cygnet.row(LogTable.id)

    def test_comparison_against_none_becomes_is_null(self):
        # Documented caveat: Predicate's None → IS NULL rewrite fires here,
        # and PG reads `(a, b) IS NULL` as "both members are null", NOT as
        # "the row itself is null".  Not special-cased on purpose.
        params: list = []
        pred = cygnet.row(AccountTable.id, AccountTable.name) == None  # noqa: E711
        sql = pred.render_sql(params)
        assert sql == "(accounts.id, accounts.name) IS NULL"
        assert params == []

    def test_inequality_against_none_becomes_is_not_null(self):
        # Sharper than the == None case: `(a, b) IS NOT NULL` is true only
        # when EVERY member is non-null, so it is NOT the negation of
        # `(a, b) IS NULL` — a row with one null member satisfies neither.
        params: list = []
        pred = cygnet.row(AccountTable.id, AccountTable.name) != None  # noqa: E711
        sql = pred.render_sql(params)
        assert sql == "(accounts.id, accounts.name) IS NOT NULL"
        assert params == []

    def test_composes_with_and(self):
        params: list = []
        pred = (cygnet.row(AccountTable.id, AccountTable.name) == (1, "Fred")) & (
            AccountTable.email == "fred@example.com"
        )
        sql = pred.render_sql(params)
        assert sql == (
            "((accounts.id, accounts.name) = ($1, $2)) AND (accounts.email = $3)"
        )
        assert params == [1, "Fred", "fred@example.com"]


class TestInPredicate:
    def test_row_in_single_pair(self):
        params: list = []
        pred = cygnet.in_(cygnet.row(AccountTable.id, AccountTable.name), [(1, "Fred")])
        sql = pred.render_sql(params)
        assert sql == "(accounts.id, accounts.name) IN (($1, $2))"
        assert params == [1, "Fred"]

    def test_row_in_many_pairs(self):
        params: list = []
        pred = cygnet.in_(
            cygnet.row(AccountTable.id, AccountTable.name),
            [(1, "Fred"), (2, "Wilma"), (3, "Barney")],
        )
        sql = pred.render_sql(params)
        assert sql == ("(accounts.id, accounts.name) IN (($1, $2), ($3, $4), ($5, $6))")
        assert params == [1, "Fred", 2, "Wilma", 3, "Barney"]

    def test_scalar_in_value_list(self):
        params: list = []
        pred = cygnet.in_(AccountTable.id, [1, 2, 3])
        sql = pred.render_sql(params)
        assert sql == "accounts.id IN ($1, $2, $3)"
        assert params == [1, 2, 3]

    def test_set_values_are_refused(self):
        # A set has no dependable order, so the emitted SQL text would vary
        # run to run — churning PG's plan cache beyond the per-batch-size
        # cost `in_` already accepts, and making rendered-SQL assertions
        # non-deterministic.
        with pytest.raises(TypeError, match="dependable order"):
            cygnet.in_(AccountTable.id, {7, 8})

    def test_dict_values_are_refused_rather_than_binding_keys(self):
        # `list({"a": 1})` yields KEYS.  Binding those silently substitutes
        # data the caller never asked for — the exact silent-wrong-query
        # class Cygnet fails loud on.
        with pytest.raises(TypeError, match="dependable order"):
            cygnet.in_(AccountTable.id, {"a": 1, "b": 2})

    def test_generator_values_are_refused(self):
        with pytest.raises(TypeError, match="dependable order"):
            cygnet.in_(AccountTable.id, (n for n in (1, 2)))

    def test_empty_values_raises(self):
        with pytest.raises(ValueError, match="non-empty"):
            cygnet.in_(AccountTable.id, [])

    def test_element_arity_mismatch_raises_naming_position(self):
        with pytest.raises(ValueError, match=r"index 1"):
            cygnet.in_(
                cygnet.row(AccountTable.id, AccountTable.name),
                [(1, "Fred"), (2,)],
            )

    def test_scalar_element_against_row_raises(self):
        with pytest.raises(ValueError, match=r"index 0"):
            cygnet.in_(cygnet.row(AccountTable.id, AccountTable.name), [1, 2])

    def test_subquery_values_raises_pointing_at_op(self):
        subq = cygnet.SELECT(FakeDB(), AccountTable.id).FROM(AccountTable)
        with pytest.raises(TypeError, match="cygnet.op"):
            cygnet.in_(AccountTable.id, subq)

    def test_non_iterable_values_raises_naming_cygnet_in(self):
        # `list(5)` already raises TypeError, but with PG-unrelated wording;
        # the message must name the Cygnet call so the seam is obvious.
        with pytest.raises(TypeError, match=r"cygnet\.in_"):
            cygnet.in_(AccountTable.id, 5)

    def test_prebuilt_row_values_are_accepted_as_elements(self):
        # Callers assembling rows programmatically (e.g. from a composite
        # key) may already hold RowValues — don't force them back into tuples.
        params: list = []
        pred = cygnet.in_(
            cygnet.row(AccountTable.id, AccountTable.name),
            [cygnet.row(1, "Fred"), cygnet.row(2, "Wilma")],
        )
        sql = pred.render_sql(params)
        assert sql == "(accounts.id, accounts.name) IN (($1, $2), ($3, $4))"
        assert params == [1, "Fred", 2, "Wilma"]

    def test_prebuilt_row_value_element_arity_is_checked(self):
        # in_ delegates to the comparison path's coercion, so the arity
        # message is _coerce's, prefixed with the offending element index.
        with pytest.raises(
            ValueError, match=r"index 0: row value has 2 elements .* has 1"
        ):
            cygnet.in_(
                cygnet.row(AccountTable.id, AccountTable.name),
                [cygnet.row(1)],
            )

    def test_string_values_raises(self):
        # A bare str is iterable; expanding it per-character is never what
        # the caller meant.
        with pytest.raises(TypeError, match="sequence"):
            cygnet.in_(AccountTable.name, "Fred")

    def test_composes_with_and(self):
        params: list = []
        pred = cygnet.in_(AccountTable.id, [1, 2]) & (AccountTable.name == "Fred")
        sql = pred.render_sql(params)
        assert sql == "(accounts.id IN ($1, $2)) AND (accounts.name = $3)"
        assert params == [1, 2, "Fred"]

    def test_negation(self):
        params: list = []
        pred = ~cygnet.in_(AccountTable.id, [1, 2])
        sql = pred.render_sql(params)
        assert sql == "NOT (accounts.id IN ($1, $2))"
        assert params == [1, 2]


class TestInQueryIntegration:
    def test_parameter_numbering_threads_through_a_select(self):
        db = FakeDB()
        sql, params = (
            cygnet.SELECT(db)
            .FROM(AccountTable)
            .WHERE(AccountTable.email == "fred@example.com")
            .WHERE(
                cygnet.in_(
                    cygnet.row(AccountTable.id, AccountTable.name),
                    [(1, "Fred"), (2, "Wilma")],
                )
            )
            .sql()
        )
        assert "accounts.email = $1" in sql
        assert "(accounts.id, accounts.name) IN (($2, $3), ($4, $5))" in sql
        assert params == ["fred@example.com", 1, "Fred", 2, "Wilma"]


class TestExports:
    def test_row_and_in_are_public(self):
        assert "row" in cygnet.__all__
        assert "in_" in cygnet.__all__


class TestRowCoercionValidation:
    """The right operand of a row comparison: what is accepted, what is not.

    The class docstring promises a mismatch "fails in Python with the two
    lengths named".  These pin the shapes that used to slip past that
    promise and reach PG (or, worse, bind in an arbitrary order) instead.
    """

    @pytest.mark.parametrize(
        "value",
        [
            pytest.param({1, 2}, id="set"),
            pytest.param({"a": 1, "b": 2}, id="dict"),
            pytest.param(range(2), id="range"),
        ],
    )
    def test_unordered_or_lazy_iterables_are_refused(self, value):
        # Right arity, wrong *kind*: none of these has a dependable element
        # order, so binding them element-wise would be silently arbitrary.
        with pytest.raises(TypeError, match="dependable order"):
            cygnet.row(AccountTable.id, AccountTable.name) == value

    def test_generator_is_refused_rather_than_consumed(self):
        gen = (x for x in (1, 2))
        with pytest.raises(TypeError, match="generator"):
            cygnet.row(AccountTable.id, AccountTable.name) == gen

    @pytest.mark.parametrize("value", [5, "ab", b"ab"])
    def test_scalar_against_wider_row_is_an_arity_error(self, value):
        # A scalar is one $N; against a 2-element row that is arity 1 vs 2.
        # `"ab"` is the pointed case — it is len-2 and iterable, but it is
        # an operand, not a pair.
        with pytest.raises(ValueError, match="has 2 elements"):
            cygnet.row(AccountTable.id, AccountTable.name) == value

    def test_scalar_against_one_element_row_is_allowed(self):
        # `(a) = $1` is just `a = $1`, so the degenerate row stays usable.
        params: list = []
        sql = (cygnet.row(AccountTable.name) == "Fred").render_sql(params)
        assert sql == "(accounts.name) = $1"
        assert params == ["Fred"]

    def test_namedtuple_is_accepted_with_arity_enforced(self):
        from collections import namedtuple

        Key = namedtuple("Key", "id name")
        params: list = []
        pred = cygnet.row(AccountTable.id, AccountTable.name) == Key(1, "Fred")
        assert pred.render_sql(params) == "(accounts.id, accounts.name) = ($1, $2)"
        assert params == [1, "Fred"]

    def test_renderable_operand_passes_through_untouched(self):
        # A column/subquery renders itself; PG checks that arity, not us.
        params: list = []
        pred = cygnet.row(AccountTable.id, AccountTable.name) == AccountTable.email
        assert pred.render_sql(params) == (
            "(accounts.id, accounts.name) = accounts.email"
        )
        assert params == []


class TestNestedRowsAgainstValues:
    """PostgreSQL cannot infer the type of a parameter inside a *nested* row
    constructor — `((a,b),c) = (($1,$2),$3)` fails to prepare with
    "could not determine data type of parameter $1", because the outer
    comparison resolves its members as `record` vs `record`, which carries no
    per-member type information.  The flat form prepares fine with no declared
    types at all.  So a nested row compared against bound values renders SQL no
    server will run, and Cygnet refuses it at the seam instead."""

    NESTED = None  # set in each test; kept out of class scope for clarity

    def _nested(self):
        return cygnet.row(
            cygnet.row(AccountTable.id, AccountTable.name), AccountTable.email
        )

    def test_nested_row_against_value_tuple_is_refused(self):
        with pytest.raises(TypeError, match="nested row"):
            self._nested() == ((1, "Fred"), "a@b")

    def test_nested_row_against_wrong_arity_is_still_refused(self):
        with pytest.raises(TypeError, match="nested row"):
            self._nested() == ((1, 2, 3), "a@b")

    def test_nested_row_in_list_is_refused(self):
        # in_ routes elements through the same coercion, so it agrees with ==
        # rather than silently binding the inner tuple as one parameter.
        with pytest.raises(TypeError, match="nested row"):
            cygnet.in_(self._nested(), [((1, "Fred"), "a@b")])

    def test_nested_row_against_columns_is_allowed(self):
        # No bound parameters, so nothing needs inferring — PG prepares this.
        params: list = []
        pred = self._nested() == cygnet.row(
            cygnet.row(LogTable.id, LogTable.message), LogTable.account_id
        )
        sql = pred.render_sql(params)
        assert sql == (
            "((accounts.id, accounts.name), accounts.email) = "
            "((log_entries.id, log_entries.message), log_entries.account_id)"
        )
        assert params == []


class TestNullInValues:
    """`IN` never matches NULL: `(a, b) IN ((NULL, 'z'))` returns zero rows
    even when a (NULL, 'z') row exists.  This is B6/OQ7 — the trap the
    `== None` -> IS NULL rewrite exists to prevent — so `in_` refuses it
    rather than emitting a query that silently matches nothing."""

    def test_none_in_scalar_value_list_is_refused(self):
        with pytest.raises(ValueError, match=r"index 1"):
            cygnet.in_(AccountTable.id, [1, None])

    def test_none_inside_a_row_value_is_refused(self):
        with pytest.raises(ValueError, match=r"index 0"):
            cygnet.in_(cygnet.row(AccountTable.id, AccountTable.name), [(None, "z")])

    def test_none_message_explains_why(self):
        with pytest.raises(ValueError, match="never matches NULL"):
            cygnet.in_(AccountTable.id, [None])


class TestRowArithmeticIsRefused:
    """Row values compare; they do not do arithmetic.  `(a, b) + $1` is not
    valid PostgreSQL, and inheriting the operators unchanged from _InfixOps
    emitted it silently."""

    @pytest.mark.parametrize(
        "apply",
        [
            lambda r: r + 1,
            lambda r: r - 1,
            lambda r: r * 1,
            lambda r: r / 1,
            lambda r: r % 1,
        ],
    )
    def test_arithmetic_operators_raise(self, apply):
        with pytest.raises(TypeError, match="only supports comparison"):
            apply(cygnet.row(AccountTable.id, AccountTable.name))

    @pytest.mark.parametrize(
        "apply",
        [
            lambda r: 1 + r,
            lambda r: 1 - r,
            lambda r: 1 * r,
        ],
    )
    def test_reflected_arithmetic_operators_raise(self, apply):
        with pytest.raises(TypeError, match="only supports comparison"):
            apply(cygnet.row(AccountTable.id, AccountTable.name))


class TestRowValueDirectConstruction:
    def test_empty_rowvalue_is_rejected_at_construction(self):
        # `row()` guards this, but RowValue is a documented public symbol
        # (ARCHITECTURE module map) and `()` is a PG syntax error.
        from cygnet.expression import RowValue

        with pytest.raises(TypeError, match="at least one"):
            RowValue(())


class TestInLeftOperandValidation:
    @pytest.mark.parametrize("left", [(1, 2), [1, 2]])
    def test_bare_sequence_left_points_at_row(self, left):
        # The likely slip, since `row(a, b) == (1, 2)` accepts a bare tuple
        # on the *right*.  Without a RowValue the columns would be bound as
        # one opaque parameter.
        with pytest.raises(TypeError, match=r"wrap it in cygnet\.row"):
            cygnet.in_(left, [(1, 7), (1, 9)])

    def test_column_left_is_still_accepted(self):
        params: list = []
        pred = cygnet.in_(AccountTable.id, [1, 2])
        assert pred.render_sql(params) == "accounts.id IN ($1, $2)"
        assert params == [1, 2]


class TestNullMemberInComparison:
    """A `None` *inside* a compared operand is refused on all six
    comparisons, matching `in_`.

    `Predicate`'s None -> IS NULL rewrite fires only when the *whole* right
    operand is None, so a None member is just another bound parameter and
    `(a, b) = ($1, $2)` evaluates to NULL — never true — for exactly the row
    the caller wanted.  Verified against PG: with a table holding the row
    `(NULL, 'z')`, `WHERE (a, b) = (NULL, 'z')` returns no rows, and the
    predicate itself IS NULL rather than false.
    """

    @pytest.mark.parametrize(
        "compare",
        [
            lambda r, v: r == v,
            lambda r, v: r != v,
            lambda r, v: r < v,
            lambda r, v: r > v,
            lambda r, v: r <= v,
            lambda r, v: r >= v,
        ],
        ids=["eq", "ne", "lt", "gt", "le", "ge"],
    )
    def test_none_member_is_refused_by_every_comparison(self, compare):
        # Ordering comparisons are included even though PG can sometimes
        # decide one before reaching the NULL member: "sometimes defined,
        # depending on the data" is not a usable rule, and a keyset cursor
        # carrying a NULL is broken from that member onward regardless.
        row = cygnet.row(AccountTable.id, AccountTable.name)
        with pytest.raises(ValueError, match="is None"):
            compare(row, (None, "z"))

    def test_message_names_the_offending_member_and_the_way_out(self):
        with pytest.raises(ValueError, match=r"index 1 is None"):
            cygnet.row(AccountTable.id, AccountTable.name) == (1, None)
        with pytest.raises(ValueError, match=r"is_null\(\)"):
            cygnet.row(AccountTable.id, AccountTable.name) == (1, None)

    def test_none_in_a_list_operand_is_refused_too(self):
        # tuple and list coerce through the same arm.
        with pytest.raises(ValueError, match="is None"):
            cygnet.row(AccountTable.id, AccountTable.name) == [None, "z"]

    def test_none_in_a_prebuilt_row_operand_is_refused(self):
        with pytest.raises(ValueError, match=r"index 0 is None"):
            cygnet.row(AccountTable.id, AccountTable.name) == cygnet.row(None, "z")

    def test_none_in_a_one_element_row_is_refused(self):
        # `(a) = ($1)` with NULL bound is the same trap at arity 1, and the
        # caller almost certainly meant `is_null()`.
        with pytest.raises(ValueError, match="is None"):
            cygnet.row(AccountTable.id) == (None,)

    def test_whole_operand_none_still_becomes_is_null(self):
        # The boundary: refusing a None *member* must not disturb the
        # documented whole-operand rewrite, which returns before coercion.
        params: list = []
        pred = cygnet.row(AccountTable.id, AccountTable.name) == None  # noqa: E711
        assert pred.render_sql(params) == "(accounts.id, accounts.name) IS NULL"
        assert params == []

    def test_arity_is_still_checked_first(self):
        # A wrong-length operand that also contains a None reports the
        # arity, which is the more fundamental mistake.
        with pytest.raises(ValueError, match="2 element"):
            cygnet.row(AccountTable.id, AccountTable.name) == (None,)

    def test_rows_without_none_are_unaffected(self):
        params: list = []
        pred = cygnet.row(AccountTable.id, AccountTable.name) == (1, "Fred")
        assert pred.render_sql(params) == "(accounts.id, accounts.name) = ($1, $2)"
        assert params == [1, "Fred"]

    def test_column_operands_are_unaffected(self):
        # `_contains_none` must not trip on renderables.
        params: list = []
        pred = cygnet.row(AccountTable.id, AccountTable.name) == cygnet.row(
            LogTable.id, LogTable.message
        )
        assert pred.render_sql(params) == (
            "(accounts.id, accounts.name) = (log_entries.id, log_entries.message)"
        )
        assert params == []

    def test_in_and_comparison_now_agree_on_the_same_operand(self):
        # The defect was the disagreement: `in_` refused this operand while
        # `==` accepted it and emitted a never-true predicate.
        row = cygnet.row(AccountTable.id, AccountTable.name)
        with pytest.raises(ValueError):
            cygnet.in_(row, [(None, "z")])
        with pytest.raises(ValueError):
            row == (None, "z")

    def test_in_row_path_reports_both_element_and_member_index(self):
        # in_ delegates to _coerce, so the message carries the element
        # index it adds plus the member index _coerce found.
        with pytest.raises(ValueError, match=r"index 1: row value member at index 0"):
            cygnet.in_(
                cygnet.row(AccountTable.id, AccountTable.name),
                [(1, "Fred"), (None, "z")],
            )

    def test_in_scalar_path_still_has_its_own_message(self):
        # The scalar left operand never reaches _coerce, so in_'s own guard
        # is what covers it.
        with pytest.raises(ValueError, match="never matches NULL"):
            cygnet.in_(AccountTable.id, [1, None])


class TestInParameterCeiling:
    """PostgreSQL's wire protocol caps one statement at 65535 bound
    parameters (GH #24).

    These pin the guard's boundary, not what a server will execute.
    Passing the check means the IN-list fits the *protocol* on its own; it
    is a lower bound three ways over, since the rest of the statement
    shares the budget, asyncpg caps a statement at 32767 arguments, and a
    row-valued IN list exhausts max_stack_depth well under ten thousand
    elements (the
    parser rewrites it into one nested OR level per element).  So a value
    here that "still builds" is asserting Cygnet's arithmetic, not that PG
    would run it.
    """

    def test_row_over_the_ceiling_names_the_count_and_the_limit(self):
        # A 2-column row: 32768 rows * 2 params/row = 65536, one over the
        # protocol ceiling.  (A real server refuses a row-valued IN list
        # far below this — see the class docstring — but that threshold is
        # configuration-dependent and not what this guard measures.)
        values = [(i, "x") for i in range(32768)]
        with pytest.raises(ValueError, match=r"65536 parameters.*65535"):
            cygnet.in_(cygnet.row(AccountTable.id, AccountTable.name), values)

    def test_scalar_over_the_ceiling_points_at_arrays_any(self):
        with pytest.raises(ValueError, match=r"arrays\.any"):
            cygnet.in_(AccountTable.id, list(range(65536)))

    def test_row_over_the_ceiling_points_at_chunking_not_arrays_any(self):
        values = [(i, "x") for i in range(32768)]
        with pytest.raises(ValueError, match="chunking is safe") as exc_info:
            cygnet.in_(cygnet.row(AccountTable.id, AccountTable.name), values)
        assert "arrays.any" not in str(exc_info.value)

    def test_just_under_the_ceiling_still_builds(self):
        # 32767 rows * 2 params/row = 65534 — one under the ceiling, so the
        # guard lets it through.  Deliberately not a claim that PG would
        # execute it: at this size the parser's nested-OR rewrite blows
        # max_stack_depth long before the wire protocol complains.
        values = [(i, "x") for i in range(32767)]
        cygnet.in_(cygnet.row(AccountTable.id, AccountTable.name), values)

    def test_ceiling_off_by_one_is_exact(self):
        # Scalar left: k=1, so the value count IS the parameter count —
        # the cleanest way to pin the boundary precisely.  65535 is the
        # largest count the wire protocol's int16 can carry; 65536 tips it
        # over.
        cygnet.in_(AccountTable.id, list(range(65535)))
        with pytest.raises(ValueError, match="65535"):
            cygnet.in_(AccountTable.id, list(range(65536)))

    def test_renderable_members_are_not_counted_as_parameters(self):
        # Each element's second member is a column: it renders in place and
        # binds nothing, so an element costs 1 parameter, not 2 — doubling
        # the effective row cap over a rectangular len(values) * k
        # assumption. A naive multiply would reject this (65535 * 2 =
        # 131070); counting actual parameters accepts it, since only 65535
        # are actually bound. This is what proves the check counts
        # parameters rather than multiplying by arity.  See
        # test_renderables_that_bind_are_counted for the other half: a
        # member that renders is not automatically free.
        values = [(i, AccountTable.email) for i in range(65535)]
        cygnet.in_(cygnet.row(AccountTable.id, AccountTable.name), values)

    def test_renderables_that_bind_are_counted(self):
        # The complement of the test above, and the reason the count is
        # taken by rendering rather than by inspecting shape: a member can
        # render in place AND bind a parameter of its own.  Treating every
        # renderable as free let an oversized list through — the exact
        # failure this check exists to prevent, just further downstream.
        from cygnet.expression import RowValue

        binding = cygnet.fn("lower")("x")
        probe: list = []
        binding.render_sql(probe)
        assert len(probe) == 1, "premise: fn(...) binds one parameter"
        assert RowValue._param_count(binding) == 1

        with pytest.raises(ValueError, match="65536 parameters"):
            cygnet.in_(AccountTable.id, [cygnet.fn("lower")("x")] * 65536)

    def test_chunking_advice_does_not_recommend_or_ing(self):
        # OR-ing chunks into one statement binds every parameter of both, so
        # it reproduces the failure being reported rather than fixing it.
        with pytest.raises(ValueError) as exc_info:
            cygnet.in_(
                cygnet.row(AccountTable.id, AccountTable.name),
                [(i, "x") for i in range(32768)],
            )
        assert "separate" in str(exc_info.value)


class TestInNoneElementUnderRowLeft:
    """A bare `None` *element* (as opposed to a None inside a tuple) under a
    row-value left operand.

    `_coerce` returns a whole-operand None untouched — that is the shape
    `row(...) == None` legitimately rewrites to `IS NULL` — so it is `in_`'s
    own guard, not `_coerce`'s, that has to catch this one.  The two guards
    overlap deliberately: were the second an `elif`, this shape would reach
    neither and render `(a, b) IN ($1)` with NULL bound.
    """

    def test_bare_none_element_is_refused(self):
        with pytest.raises(ValueError, match="never matches NULL"):
            cygnet.in_(cygnet.row(AccountTable.id, AccountTable.name), [None])

    def test_bare_none_among_valid_elements_is_refused(self):
        with pytest.raises(ValueError, match=r"index 1"):
            cygnet.in_(cygnet.row(AccountTable.id, AccountTable.name), [(1, "x"), None])

    def test_none_in_a_one_column_row_list_is_refused(self):
        # The natural shape for a single-column key: elements are scalars, so
        # a None element is indistinguishable from the scalar case and has to
        # be refused the same way.
        with pytest.raises(ValueError, match="never matches NULL"):
            cygnet.in_(cygnet.row(AccountTable.id), [1, None])

    def test_valid_row_lists_are_unaffected(self):
        params: list = []
        pred = cygnet.in_(
            cygnet.row(AccountTable.id, AccountTable.name), [(1, "x"), (2, "y")]
        )
        assert pred.render_sql(params) == (
            "(accounts.id, accounts.name) IN (($1, $2), ($3, $4))"
        )
        assert params == [1, "x", 2, "y"]
