"""Review decisions belong to the period and survive re-runs."""
from decimal import Decimal as D

import pytest

from core import services
from core.models import RawImportRow, ReviewDecision

pytestmark = pytest.mark.django_db

E03_KEY = "MISSED_CLOCK_OUT|E03|2026-09-10|A"
E07_KEY = "ABSENCE_UNINFORMED|E07|2026-09-17|B"
E16_KEY = "LOAN_CAPPED|E16|2026-09-20|"


@pytest.fixture
def period(tmp_path):
    period = services.seed_demo(output_dir=tmp_path)
    services.run_payroll(period)
    return period


def line(period, employee_id):
    return services.current_run(period).lines.get(employee__employee_id=employee_id)


def exc(period, key):
    return services.current_run(period).exceptions.get(exception_key=key)


def test_approving_missed_clock_out_with_hours_raises_gross(period):
    before = line(period, "E03").gross
    services.decide(period, E03_KEY, "approve_with_value", {"hours": "8"}, "confirmed with manager")
    after = line(period, "E03")
    assert after.gross - before == 8 * D("15.50")
    assert any(s["label"].startswith("Reviewer adjustment") for s in after.trail)
    record = exc(period, E03_KEY)
    assert (record.status, record.review_note, record.adjusted) == ("approved", "confirmed with manager", True)


def test_decision_survives_rerun(period):
    services.decide(period, E03_KEY, "approve_with_value", {"hours": "8"}, "")
    gross = line(period, "E03").gross
    services.run_payroll(period)
    services.run_payroll(period)
    assert line(period, "E03").gross == gross
    assert exc(period, E03_KEY).status == "approved"


def test_undo_restores_the_original_amount(period):
    original = line(period, "E03").gross
    services.decide(period, E03_KEY, "approve_with_value", {"hours": "8"}, "")
    services.undo_decision(period, E03_KEY)
    after = line(period, "E03")
    assert after.gross == original
    assert not any(s["label"].startswith("Reviewer adjustment") for s in after.trail)
    assert exc(period, E03_KEY).status == "open"


def test_reclassifying_uninformed_absence_as_paid_leave(period):
    before = line(period, "E07")
    assert (before.absence_deductions, before.penalties) == (D("220.00"), D("25.00"))
    services.decide(period, E07_KEY, "approve_with_value", {"leave_type": "sick"}, "called in sick")
    after = line(period, "E07")
    assert (after.absence_deductions, after.penalties) == (D("0.00"), D("0.00"))
    assert after.gross - before.gross == D("220.00")


def test_loan_capped_rejects_amount_above_installment(period):
    with pytest.raises(services.DecisionError, match="installment"):
        services.decide(period, E16_KEY, "approve_with_value", {"amount": "400.01"})
    assert not ReviewDecision.objects.exists()
    services.decide(period, E16_KEY, "approve_with_value", {"amount": "400.00"})
    assert line(period, "E16").loan_deductions == D("400.00")


@pytest.mark.parametrize("key, value, message", [
    (E03_KEY, {"hours": "25"}, "between 0 and 24"),
    (E03_KEY, {"hours": "abc"}, "number"),
    (E07_KEY, {"leave_type": "holiday"}, "not a leave type"),
    ("WRONG_LOCATION|E10|2026-09-16|B", {"location": "C"}, "no pay rate"),
    ("EXIT_FINAL_SETTLEMENT|E13|2026-09-11|", {}, "only be approved as is"),
])
def test_invalid_values_are_rejected(period, key, value, message):
    with pytest.raises(services.DecisionError, match=message):
        services.decide(period, key, "approve_with_value", value)


def test_info_exceptions_need_no_decision(period):
    info = services.current_run(period).exceptions.filter(severity="info").first()
    with pytest.raises(services.DecisionError, match="no decision needed"):
        services.decide(period, info.exception_key, "approve")


def test_approve_as_is_keeps_the_engine_result(period):
    gross = line(period, "E03").gross
    services.decide(period, E03_KEY, "approve", {"hours": "8"}, "really did leave early")
    assert line(period, "E03").gross == gross
    assert ReviewDecision.objects.get().value is None


def test_clear_decisions_resets_everything(period):
    services.decide(period, E03_KEY, "approve_with_value", {"hours": "8"})
    services.decide(period, E07_KEY, "approve")
    services.clear_decisions(period)
    assert not ReviewDecision.objects.exists()
    assert services.current_run(period).exceptions.filter(severity="needs_review", status="open").count() == 7


def test_raw_rows_are_kept_with_parse_errors(period):
    rows = RawImportRow.objects.filter(batch__location__code="A")
    assert rows.count() == period.imports.get(location__code="A").row_count
    bad = rows.get(row_number=36)
    assert bad.raw == {"EmpID": "1002", "Date": "2026-09-09", "Time": "9:6O", "Type": "IN"}
    assert "9:6O" in bad.parse_error
    assert period.imports.get(location__code="C").unknown_columns == ["Dept"]
