"""Exempt salaried partial days: no deduction by default, an attendance item to discuss instead."""
from decimal import Decimal as D

import pytest

from core.engine.run import run_payroll

from .helpers import only

E01_MESSAGE = ("E01 Avery Holt worked 3.5 of 9 scheduled hours on Wed Sep 9. Pay not reduced: partial-day absences "
               "can't be deducted from exempt salaried pay. Worth a conversation, or recording it against paid leave.")


@pytest.fixture(scope="module")
def deduction_on(inputs, rules):
    return run_payroll(inputs, rules.with_changes(salaried_partial_day_deduction=True))


def test_partial_day_is_an_attendance_item_with_no_deduction(result):
    ex = only(result.exceptions_for("PARTIAL_DAY", "E01"))
    assert (ex.severity, str(ex.date), ex.location_code) == ("attendance", "2026-09-09", "A")
    assert ex.message == E01_MESSAGE
    assert ex.context["occurrence_label"] == "1st partial day this period"
    assert not result.exceptions_for("HALF_DAY")


def test_trail_explains_that_no_deduction_was_made(result):
    line = result.line_for("E01")
    step = only([s for s in line.trail if s.label == "Partial day 2026-09-09"])
    assert step.amount is None
    assert step.formula == ("worked 3.50 h of 9.00 h scheduled: no deduction. Partial-day absences can't be "
                            "deducted from exempt salaried pay (rules: salaried_partial_day_deduction is off)")
    assert not [s for s in line.trail if s.label.startswith("Half day")]


def test_default_raises_e01_gross_by_the_old_half_day(result, deduction_on):
    on, off = deduction_on.line_for("E01"), result.line_for("E01")
    assert (on.absence_deductions, off.absence_deductions) == (D("100.00"), D("0.00"))
    assert (on.gross, off.gross) == (D("1900.00"), D("2000.00"))
    # Custom deductions follow gross: 3% retirement +$3.00, 1.5% table bracket +$1.50.
    assert off.custom_deductions - on.custom_deductions == D("4.50")
    assert off.net - on.net == D("95.50")
    assert result.totals["gross"] - deduction_on.totals["gross"] == D("100.00")
    assert result.totals["by_location"]["A"]["gross"] - deduction_on.totals["by_location"]["A"]["gross"] == D("100.00")
    assert result.totals["by_department"]["Floor"]["gross"] - \
        deduction_on.totals["by_department"]["Floor"]["gross"] == D("100.00")


def test_switched_on_keeps_the_old_numbers(deduction_on):
    ex = only(deduction_on.exceptions_for("HALF_DAY", "E01"))
    assert (ex.severity, ex.resolution) == ("info", "Deducted $100.00")
    assert not deduction_on.exceptions_for("PARTIAL_DAY")
    assert (deduction_on.totals["gross"], deduction_on.totals["net"]) == (D("24349.99"), D("22722.59"))
    assert deduction_on.totals["exceptions_by_severity"] == {"needs_review": 6, "info": 14, "attendance": 2}


def test_both_switches_on_and_e10_at_store_b_give_the_numbers_before_spec_5(inputs, rules):
    from datetime import date

    from core.engine.types import Adjustments, LocationOverride

    at_b = Adjustments(location_overrides=[LocationOverride("E10", date(2026, 9, 16), "B")])
    res = run_payroll(inputs, rules.with_changes(salaried_partial_day_deduction=True,
                                                 attendance_penalties_enabled=True), at_b)
    assert (res.totals["gross"], res.totals["net"]) == (D("24341.99"), D("22684.91"))


def test_salaried_full_day_absence_is_still_deducted(result):
    line = result.line_for("E07")
    assert line.absence_deductions == D("220.00")
    assert only(result.exceptions_for("ABSENCE_UNINFORMED", "E07")).message.endswith(
        "One day's pay deducted ($220.00).")
