"""Review gate: a run is finalized only after every exception that needs a human has been handled."""
from decimal import Decimal as D

import pytest

from core import services
from core.models import LoanAdvance, PayrollExceptionRecord, PayrollRun

pytestmark = pytest.mark.django_db


@pytest.fixture
def run(tmp_path):
    return services.run_payroll(services.seed_demo(output_dir=tmp_path))


def needs_review(run):
    return run.exceptions.filter(severity="needs_review")


def test_finalize_blocked_while_needs_review_is_open(run):
    first = needs_review(run).first()
    for x in needs_review(run).exclude(pk=first.pk):
        services.review_exception(x.pk, "approve")
    with pytest.raises(services.FinalizeRefused) as refused:
        services.finalize(run)
    assert "1 needs_review exception(s) still open" in str(refused.value)
    run.refresh_from_db()
    assert run.status == PayrollRun.DRAFT


def test_finalize_allowed_after_approval_and_override(run):
    approve, *rest = needs_review(run)
    services.review_exception(approve.pk, "approve", "Confirmed with store manager")
    for x in rest:
        services.review_exception(x.pk, "override", "Paid per manager's timesheet")
    finalized = services.finalize(run)
    assert finalized.status == PayrollRun.FINALIZED and finalized.finalized_at is not None
    assert set(needs_review(run).values_list("status", flat=True)) == {"approved", "overridden"}


def test_override_needs_a_note(run):
    with pytest.raises(services.PayrollError):
        services.review_exception(needs_review(run).first().pk, "override", "  ")


def test_finalize_posts_loan_deductions_and_locks_the_period(run):
    for x in needs_review(run):
        services.review_exception(x.pk, "approve")
    services.finalize(run)
    advance = LoanAdvance.objects.get(employee__employee_id="E12")
    capped = LoanAdvance.objects.get(employee__employee_id="E16")
    assert advance.paid_to_date == D("300.00")  # 150 before + 150 this period
    assert capped.paid_to_date == D("276.00")  # capped; 1,724.00 carries over
    with pytest.raises(services.PayrollError):
        services.run_payroll(run.period)
    with pytest.raises(services.PayrollError):
        services.review_exception(needs_review(run).first().pk, "approve")


def test_rerun_replaces_the_draft(run):
    again = services.run_payroll(run.period)
    assert list(PayrollRun.objects.values_list("pk", flat=True)) == [again.pk]
    assert not PayrollExceptionRecord.objects.filter(run_id=run.pk).exists()


def test_missing_store_blocks_finalize_even_after_review(tmp_path):
    run = services.run_payroll(services.seed_demo(missing_store="B", output_dir=tmp_path))
    missing = run.exceptions.get(code="STORE_FILE_MISSING")
    assert (missing.severity, missing.location.code) == ("blocking", "B")
    for x in run.exceptions.exclude(severity="info"):
        services.review_exception(x.pk, "approve")
    with pytest.raises(services.FinalizeRefused) as refused:
        services.finalize(run)
    assert "STORE_FILE_MISSING" in str(refused.value)
