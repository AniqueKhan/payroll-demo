"""Reviewer adjustments in the pure engine (no database)."""
from datetime import date
from decimal import Decimal as D

import pytest

from core.engine.run import run_payroll
from core.engine.types import Adjustments, HoursOverride, LeaveOverride, LoanOverride, LocationOverride

from .helpers import only


def adjusted_steps(line):
    return [s for s in line.trail if s.is_adjustment]


def test_no_adjustments_changes_nothing(inputs, rules, result):
    again = run_payroll(inputs, rules, Adjustments())
    assert again.totals == result.totals
    assert not any(e.adjusted for e in again.exceptions)


def test_hours_override_pays_the_day_and_keeps_the_exception_visible(inputs, rules, result):
    adj = Adjustments(hours_overrides=[HoursOverride("E03", date(2026, 9, 10), "A", D("8.00"), "manager confirmed")])
    res = run_payroll(inputs, rules, adj)
    before, after = result.line_for("E03"), res.line_for("E03")
    assert after.gross - before.gross == D("124.00")  # 8 h x $15.50
    step = only(adjusted_steps(after))
    assert step.label == "Reviewer adjustment: hours" and "manager confirmed" in step.formula
    ex = only(res.exceptions_for("MISSED_CLOCK_OUT", "E03"))
    assert ex.severity == "needs_review" and ex.adjusted
    assert ex.resolution == "Reviewer set 8.00 h: manager confirmed"


def test_hours_override_on_an_odd_punch_day(inputs, rules, result):
    adj = Adjustments(hours_overrides=[HoursOverride("E08", date(2026, 9, 9), "B", D("7.50"))])
    res = run_payroll(inputs, rules, adj)
    assert res.line_for("E08").gross - result.line_for("E08").gross == D("112.50")  # 7.5 h x $15.00
    assert only(res.exceptions_for("ODD_PUNCH_COUNT", "E08")).adjusted


def test_location_override_moves_pay_and_cost(inputs, rules, result):
    adj = Adjustments(location_overrides=[LocationOverride("E10", date(2026, 9, 16), "A", "covered at A")])
    line = run_payroll(inputs, rules, adj).line_for("E10")
    assert line.gross - result.line_for("E10").gross == D("8.00")  # 8 h at $17 (A) instead of $16 (B)
    assert line.by_location["A"]["regular_pay"] == D("136.00")


def test_leave_override_reclassifies_uninformed_absence(inputs, rules, result):
    adj = Adjustments(leave_overrides=[LeaveOverride("E07", date(2026, 9, 17), "sick", "called in")])
    res = run_payroll(inputs, rules, adj)
    before, after = result.line_for("E07"), res.line_for("E07")
    assert (before.absence_deductions, before.penalties) == (D("220.00"), D("25.00"))
    assert (after.absence_deductions, after.penalties) == (D("0.00"), D("0.00"))
    ex = only(res.exceptions_for("ABSENCE_UNINFORMED", "E07"))
    assert ex.adjusted and "sick leave" in ex.resolution


def test_loan_override_sets_the_deduction(inputs, rules):
    adj = Adjustments(loan_overrides=[LoanOverride("E16", "loan", D("150.00"), "hardship")])
    res = run_payroll(inputs, rules, adj)
    line = res.line_for("E16")
    assert line.loan_deductions == D("150.00")
    step = only(adjusted_steps(line))
    assert step.stage == "finance" and step.amount == D("150.00")
    assert only(res.exceptions_for("LOAN_CAPPED", "E16")).adjusted


def test_loan_override_above_installment_is_refused(inputs, rules):
    adj = Adjustments(loan_overrides=[LoanOverride("E16", "loan", D("400.01"))])
    with pytest.raises(ValueError):
        run_payroll(inputs, rules, adj)


def test_override_for_a_day_with_no_punches_is_ignored(inputs, rules, result):
    adj = Adjustments(hours_overrides=[HoursOverride("E03", date(2026, 9, 12), "A", D("8.00"))])
    assert run_payroll(inputs, rules, adj).line_for("E03").gross == result.line_for("E03").gross


def test_trail_steps_carry_their_stage(result):
    stages = [s.stage for s in result.line_for("E16").trail]
    assert stages[0] == "hr" and "finance" in stages and stages[-1] == "custom"
    assert stages == sorted(stages, key=["hr", "finance", "custom"].index)


def test_exception_rows_and_keys(result):
    ex = only(result.exceptions_for("MISSED_CLOCK_OUT", "E03"))
    assert ex.key == "MISSED_CLOCK_OUT|E03|2026-09-10|A"
    assert ex.rows == [("A", 51)] and ex.context == {"suggested_hours": "8.00"}
