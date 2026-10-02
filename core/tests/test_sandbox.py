"""Per-visitor sandboxes: every visitor starts from the clean baseline and only changes their own copy."""
from datetime import timedelta
from decimal import Decimal as D
from io import StringIO

import pytest
from django.core.management import call_command
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from core import services
from core.models import (
    DemoSandbox, PayrollExceptionRecord, PayrollLineRecord, PayrollRun, ReviewDecision,
)

pytestmark = pytest.mark.django_db

E03_KEY = "MISSED_CLOCK_OUT|E03|2026-09-10|A"
BASE_GROSS = D("24217.99")


@pytest.fixture
def period(tmp_path):
    period = services.seed_demo(output_dir=tmp_path)
    services.run_payroll(period)
    return period


def approve_e03(client, hours="8"):
    return client.post(reverse("core:decide"), {"exception_key": E03_KEY, "action": "approve_with_value",
                                                "hours": hours, "note": "visitor"})


def sandbox_of(client):
    return DemoSandbox.objects.get(pk=client.session["sandbox_id"])


def baseline_state(period):
    run = services.current_run(period)
    return {
        "run": run.pk,
        "status": run.status,
        "totals": run.totals,
        "lines": list(run.lines.order_by("employee__employee_id").values_list("employee__employee_id", "gross",
                                                                              "net")),
        "exceptions": list(run.exceptions.order_by("id").values_list("exception_key", "status")),
        "decisions": list(ReviewDecision.objects.filter(sandbox=None).values_list("exception_key", "action")),
    }


def page(client, name, **kw):
    return client.get(reverse(f"core:{name}", kwargs=kw or None)).content.decode()


def test_new_visitor_sees_the_baseline(client, period):
    overview = page(client, "overview")
    assert "7 items need your review" in overview and "$24,217.99" in overview and "Clean demo" in overview
    assert "E03" in page(client, "exceptions") and "Approve with these hours" in page(client, "exceptions")


def test_browsing_creates_no_sandbox(client, period):
    for name, kw in [("overview", {}), ("inputs", {}), ("exceptions", {}), ("payroll", {}),
                     ("employee", {"employee_id": "E03"})]:
        assert client.get(reverse(f"core:{name}", kwargs=kw or None)).status_code == 200
    client.get("/runs/1/payroll/")
    assert not DemoSandbox.objects.exists()
    assert PayrollRun.objects.count() == 1


def test_first_approval_creates_exactly_one_sandbox_and_changes_totals(client, period):
    approve_e03(client)
    assert DemoSandbox.objects.count() == 1
    run = services.current_run(period, sandbox_of(client))
    assert D(run.totals["gross"]) == BASE_GROSS + D("124.00")
    approve_e03(client, hours="7")  # a second action reuses the same sandbox
    assert DemoSandbox.objects.count() == 1
    assert "$24,326.49" in page(client, "overview")  # 7 h x $15.50 = $108.50 over the baseline


def test_sandbox_run_starts_identical_to_the_baseline(period):
    sandbox = services.create_sandbox(period)
    base, mine = services.current_run(period), services.current_run(period, sandbox)
    assert mine.pk != base.pk and mine.sandbox == sandbox
    assert mine.totals == base.totals
    fields = ("employee__employee_id", "gross", "net", "hours", "overtime_hours", "trail", "by_location")
    assert list(mine.lines.order_by("employee__employee_id").values_list(*fields)) == \
        list(base.lines.order_by("employee__employee_id").values_list(*fields))
    fields = ("exception_key", "severity", "status", "message", "rows", "context")
    assert list(mine.exceptions.order_by("id").values_list(*fields)) == \
        list(base.exceptions.order_by("id").values_list(*fields))


def test_two_visitors_are_isolated(period):
    a, b = Client(), Client()
    approve_e03(a)
    assert "6 items need your review" in page(a, "overview")

    # B still sees E03 open and the original totals.
    overview_b = page(b, "overview")
    assert "7 items need your review" in overview_b and "$24,217.99" in overview_b
    assert 'value="approve_with_value"' in page(b, "exceptions").split('id="exc-MISSED_CLOCK_OUT-E03')[1][:4000]
    assert "Reviewer adjustment" not in page(b, "employee", employee_id="E03")

    # B acts too: two sandboxes, each with only its own decision.
    b.post(reverse("core:decide"), {"exception_key": "ABSENCE_UNINFORMED|E07|2026-09-17|B", "action": "approve"})
    assert DemoSandbox.objects.count() == 2
    assert list(sandbox_of(a).decisions.values_list("exception_key", flat=True)) == [E03_KEY]
    assert list(sandbox_of(b).decisions.values_list("exception_key", flat=True)) == [
        "ABSENCE_UNINFORMED|E07|2026-09-17|B"]
    assert "6 items need your review" in page(a, "overview") and "6 items need your review" in page(b, "overview")
    assert D(services.current_run(period, sandbox_of(b)).totals["gross"]) == BASE_GROSS


def test_baseline_is_unchanged_by_any_visitor_action(client, period):
    before = baseline_state(period)
    approve_e03(client)
    client.post(reverse("core:undo"), {"exception_key": E03_KEY})
    for key in services.current_run(period).exceptions.filter(severity="needs_review") \
            .values_list("exception_key", flat=True):
        client.post(reverse("core:decide"), {"exception_key": key, "action": "approve"})
    client.post(reverse("core:finalize"))
    client.post(reverse("core:reopen"))
    client.post(reverse("core:clear_decisions"))
    client.post(reverse("core:start_over"))
    assert baseline_state(period) == before


def test_no_url_exposes_another_sandbox(period):
    a, b = Client(), Client()
    approve_e03(a)
    a_run = services.current_run(period, sandbox_of(a))
    # Old run-id URLs ignore the id and show the visitor's own view.
    for path in (f"/runs/{a_run.pk}/", f"/runs/{a_run.pk}/payroll/", f"/runs/{a_run.pk}/employees/E03/",
                 f"/runs/{a_run.pk}/exceptions/"):
        r = b.get(path, follow=True)
        assert r.status_code == 200
        assert "Reviewer adjustment" not in r.content.decode() and "Your copy" not in r.content.decode()
    assert b.get(f"/runs/{a_run.pk}//evil.example.com/").url == "/"
    # A forged session id that is not a sandbox, or another visitor's id guessed wrong, is just ignored.
    session = b.session
    session["sandbox_id"] = "not-a-uuid"
    session.save()
    assert "Clean demo" in page(b, "overview")
    assert not DemoSandbox.objects.exclude(pk=sandbox_of(a).pk).exists()


def test_start_over_returns_to_the_baseline(client, period):
    approve_e03(client)
    sandbox_id = sandbox_of(client).pk
    r = client.post(reverse("core:start_over"), follow=True)
    html = r.content.decode()
    assert "Started over" in html and "7 items need your review" in html and "Clean demo" in html
    assert "sandbox_id" not in client.session
    assert not DemoSandbox.objects.filter(pk=sandbox_id).exists()
    assert not PayrollRun.objects.filter(sandbox_id=sandbox_id).exists()
    assert not ReviewDecision.objects.filter(sandbox_id=sandbox_id).exists()


def test_cleanup_deletes_only_old_sandboxes_and_cascades(period):
    old, fresh = services.create_sandbox(period), services.create_sandbox(period)
    services.decide(period, E03_KEY, "approve", sandbox=old)
    services.decide(period, E03_KEY, "approve", sandbox=fresh)
    DemoSandbox.objects.filter(pk=old.pk).update(last_seen_at=timezone.now() - timedelta(hours=30))
    old_run = services.current_run(period, old)

    out = StringIO()
    call_command("cleanup_sandboxes", "--older-than-hours", "24", stdout=out)
    assert "Removed 1 sandbox older than 24 hours" in out.getvalue()
    assert list(DemoSandbox.objects.values_list("pk", flat=True)) == [fresh.pk]
    assert not PayrollRun.objects.filter(pk=old_run.pk).exists()
    assert not PayrollLineRecord.objects.filter(run_id=old_run.pk).exists()
    assert not PayrollExceptionRecord.objects.filter(run_id=old_run.pk).exists()
    assert not ReviewDecision.objects.filter(sandbox_id=old.pk).exists()
    assert services.current_run(period) is not None  # the baseline is never cleaned up


def test_session_pointing_to_a_deleted_sandbox_is_a_new_visitor(client, period):
    approve_e03(client)
    sandbox_of(client).delete()  # e.g. removed by cleanup_sandboxes
    overview = page(client, "overview")
    assert "7 items need your review" in overview and "Clean demo" in overview
    approve_e03(client)  # acting again creates a fresh sandbox
    assert DemoSandbox.objects.count() == 1
    assert "6 items need your review" in page(client, "overview")


def test_last_seen_is_updated_on_post_not_on_get(client, period):
    approve_e03(client)
    DemoSandbox.objects.update(last_seen_at=timezone.now() - timedelta(hours=5))
    page(client, "overview")
    assert sandbox_of(client).last_seen_at < timezone.now() - timedelta(hours=4)
    approve_e03(client, hours="7")
    assert sandbox_of(client).last_seen_at > timezone.now() - timedelta(minutes=1)


def test_finalize_and_reopen_in_a_sandbox_never_touch_the_baseline(period):
    from core.models import LoanAdvance

    loans_before = list(LoanAdvance.objects.order_by("pk").values_list("paid_to_date", flat=True))
    before = baseline_state(period)
    sandbox = services.create_sandbox(period)
    for key in services.current_run(period, sandbox).exceptions.filter(severity="needs_review") \
            .values_list("exception_key", flat=True):
        services.decide(period, key, "approve", sandbox=sandbox)
    assert services.finalize(period, sandbox=sandbox).status == PayrollRun.FINALIZED
    with pytest.raises(services.PayrollError):
        services.run_payroll(period, sandbox=sandbox)
    assert services.run_payroll(period).status == PayrollRun.DRAFT  # baseline still re-runs
    assert services.reopen(period, sandbox=sandbox).status == PayrollRun.DRAFT
    after = baseline_state(period)
    assert {k: v for k, v in after.items() if k != "run"} == {k: v for k, v in before.items() if k != "run"}
    # Loan balances are shared inputs: a sandbox finalize never posts to them.
    assert list(LoanAdvance.objects.order_by("pk").values_list("paid_to_date", flat=True)) == loans_before


def test_seed_demo_deletes_all_sandboxes(period, tmp_path):
    services.create_sandbox(period)
    services.seed_demo(output_dir=tmp_path / "again")
    assert not DemoSandbox.objects.exists()


def test_management_run_payroll_works_on_the_baseline(period):
    sandbox = services.create_sandbox(period)
    services.decide(period, E03_KEY, "approve", sandbox=sandbox)
    call_command("run_payroll", "--period", "2026-09-07", stdout=StringIO())
    assert services.current_run(period).exceptions.filter(severity="needs_review", status="open").count() == 7
    assert services.current_run(period, sandbox).exceptions.filter(status="approved").count() == 1
