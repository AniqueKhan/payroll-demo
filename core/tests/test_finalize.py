"""Review gate: a run is finalized only after every exception that needs a human has a decision."""
from decimal import Decimal as D

import pytest

from core import services
from core.models import LoanAdvance, PayrollExceptionRecord, PayrollRun

pytestmark = pytest.mark.django_db


@pytest.fixture
def period(tmp_path):
    period = services.seed_demo(output_dir=tmp_path)
    services.run_payroll(period)
    return period


def needs_review(run):
    return run.exceptions.filter(severity="needs_review")


def approve_all(period, skip=0):
    run = services.current_run(period)
    for key in list(needs_review(run).values_list("exception_key", flat=True))[skip:]:
        run = services.decide(period, key, "approve", note="checked")
    return run


def test_seeded_run_has_seven_items_to_review(period):
    run = services.current_run(period)
    assert needs_review(run).filter(status="open").count() == 7
    assert run.exceptions.filter(severity="needs_review").exclude(exception_key="").count() == 7


def test_finalize_blocked_while_needs_review_is_open(period):
    run = approve_all(period, skip=1)
    with pytest.raises(services.FinalizeRefused) as refused:
        services.finalize(period)
    assert "1 needs_review exception(s) still open" in str(refused.value)
    run.refresh_from_db()
    assert run.status == PayrollRun.DRAFT


def test_finalize_only_after_all_seven_have_decisions_and_locks_until_reopened(period):
    run = services.current_run(period)
    with pytest.raises(services.FinalizeRefused):
        services.finalize(period)
    run = approve_all(period)
    assert period.decisions.count() == 7
    assert set(needs_review(run).values_list("status", flat=True)) == {"approved"}
    finalized = services.finalize(period)
    assert finalized.status == PayrollRun.FINALIZED and finalized.finalized_at is not None

    with pytest.raises(services.PayrollError):
        services.run_payroll(period)
    with pytest.raises(services.PayrollError):
        services.decide(period, needs_review(run).first().exception_key, "approve")

    reopened = services.reopen(period)
    assert reopened.status == PayrollRun.DRAFT
    assert services.run_payroll(period).status == PayrollRun.DRAFT


def test_finalize_posts_loan_deductions_and_reopen_takes_them_back(period):
    approve_all(period)
    services.finalize(period)
    advance = LoanAdvance.objects.get(employee__employee_id="E12")
    capped = LoanAdvance.objects.get(employee__employee_id="E16")
    assert advance.paid_to_date == D("300.00")  # 150 before + 150 this period
    assert capped.paid_to_date == D("276.00")  # capped; 1,724.00 carries over

    services.reopen(period)
    advance.refresh_from_db()
    capped.refresh_from_db()
    assert (advance.paid_to_date, capped.paid_to_date) == (D("150.00"), D("0.00"))


def test_rerun_replaces_the_draft(period):
    first = services.current_run(period)
    again = services.run_payroll(period)
    assert list(PayrollRun.objects.values_list("pk", flat=True)) == [again.pk]
    assert not PayrollExceptionRecord.objects.filter(run_id=first.pk).exists()


def test_missing_store_blocks_finalize_and_cannot_be_approved(tmp_path):
    period = services.seed_demo(missing_store="B", output_dir=tmp_path)
    run = services.run_payroll(period)
    missing = run.exceptions.get(code="STORE_FILE_MISSING")
    assert (missing.severity, missing.location.code) == ("blocking", "B")
    with pytest.raises(services.DecisionError, match="Blocking"):
        services.decide(period, missing.exception_key, "approve")

    run = approve_all(period)
    with pytest.raises(services.FinalizeRefused) as refused:
        services.finalize(period)
    assert "STORE_FILE_MISSING" in str(refused.value)
