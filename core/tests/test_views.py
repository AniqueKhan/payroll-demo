"""The demo screens and their POST actions (session-resolved URLs, no run ids)."""
from decimal import Decimal as D

import pytest
from django.urls import reverse

from core import services
from core.models import DemoSandbox, PayrollRun

pytestmark = pytest.mark.django_db


@pytest.fixture
def period(tmp_path):
    period = services.seed_demo(output_dir=tmp_path)
    services.run_payroll(period)
    return period


def url(name, **kw):
    return reverse(f"core:{name}", kwargs=kw or None)


def my_run(client, period):
    """The run the client's session points at (its sandbox run)."""
    return services.current_run(period, DemoSandbox.objects.get(pk=client.session["sandbox_id"]))


def test_root_is_the_overview(client, period):
    r = client.get("/")
    assert r.status_code == 200 and b"Payroll overview" in r.content


def test_empty_page_without_a_run(client, db):
    r = client.get("/")
    assert r.status_code == 200 and b"seed_demo" in r.content


@pytest.mark.parametrize("name, kw", [
    ("overview", {}), ("inputs", {}), ("exceptions", {}), ("payroll", {}), ("employee", {"employee_id": "E11"}),
])
def test_every_page_renders(client, period, name, kw):
    r = client.get(url(name, **kw))
    assert r.status_code == 200
    assert b"Sep 7 to Sep 20, 2026" in r.content and b"Draft" in r.content and b"Clean demo" in r.content


def test_overview_calls_for_review_and_disables_finalize(client, period):
    html = client.get(url("overview")).content.decode()
    assert "7 items need your review" in html
    assert "disabled" in html and "7 needs_review exception(s) still open" in html


def test_inputs_mark_flagged_rows_unknown_columns_and_link_to_exceptions(client, period):
    html = client.get(url("inputs")).content.decode()
    assert 'id="row-A-36"' in html and "malformed row" in html
    assert "unknown column, ignored" in html  # store C's 'Dept'
    assert "#exc-MISSED_CLOCK_OUT-E03-2026-09-10-A" in html


def test_exceptions_queue_shows_forms_with_suggested_values(client, period):
    html = client.get(url("exceptions")).content.decode()
    assert 'id="exc-MISSED_CLOCK_OUT-E03-2026-09-10-A"' in html
    assert 'name="hours" min="0" max="24" step="0.01" value="8.00"' in html
    assert 'name="amount" min="0" max="400.00" step="0.01" value="276.00"' in html
    assert '<option value="B" selected>Store B (scheduled)</option>' in html
    assert "Acknowledge" in html  # exit settlement: approve only


def test_payroll_lists_everyone_with_markers_and_totals(client, period):
    html = client.get(url("payroll")).content.decode()
    assert html.count('class="rowlink"') == 18
    assert "Needs review" in html and "$24,217.99" in html


def test_decide_via_post_updates_totals_and_confirms(client, period):
    r = client.post(url("decide"), {"exception_key": "MISSED_CLOCK_OUT|E03|2026-09-10|A",
                                    "action": "approve_with_value", "hours": "8", "note": "ok"}, follow=True)
    assert r.status_code == 200
    html = r.content.decode()
    assert "Approved with value: Missed clock-out. 6 items still need review." in html
    assert "Your copy" in html and "Start over" in html
    assert D(my_run(client, period).totals["gross"]) == D("24217.99") + D("124.00")
    assert "Reviewer adjustment" in client.get(url("employee", employee_id="E03")).content.decode()


def test_invalid_post_shows_the_error(client, period):
    r = client.post(url("decide"), {"exception_key": "LOAN_CAPPED|E16|2026-09-20|",
                                    "action": "approve_with_value", "amount": "999"}, follow=True)
    assert "between $0.00 and the installment $400.00" in r.content.decode()


def test_blocking_cannot_be_approved_via_post(client, tmp_path):
    period = services.seed_demo(missing_store="B", output_dir=tmp_path)
    services.run_payroll(period)
    assert "Cannot be approved" in client.get(url("exceptions")).content.decode()
    r = client.post(url("decide"), {"exception_key": "STORE_FILE_MISSING|||B", "action": "approve"}, follow=True)
    assert "Blocking exceptions cannot be approved" in r.content.decode()
    assert not period.decisions.exists()


def test_undo_and_reset_via_post(client, period):
    key = "ABSENCE_UNINFORMED|E07|2026-09-17|B"
    client.post(url("decide"), {"exception_key": key, "action": "approve_with_value", "leave_type": "sick"})
    assert "Undo" in client.get(url("exceptions")).content.decode()
    client.post(url("undo"), {"exception_key": key})
    assert not period.decisions.exists()
    client.post(url("decide"), {"exception_key": key, "action": "approve"})
    client.post(url("clear_decisions"))
    assert not period.decisions.exists()


def test_undo_reset_and_reopen_without_a_sandbox_do_not_create_one(client, period):
    client.post(url("undo"), {"exception_key": "ABSENCE_UNINFORMED|E07|2026-09-17|B"})
    client.post(url("clear_decisions"))
    client.post(url("reopen"))
    client.post(url("start_over"))
    assert not DemoSandbox.objects.exists()


def test_full_walkthrough_finalize_and_reopen(client, period):
    keys = list(services.current_run(period).exceptions.filter(severity="needs_review")
                .values_list("exception_key", flat=True))
    values = {"MISSED_CLOCK_OUT": {"hours": "8"}, "ODD_PUNCH_COUNT": {"hours": "7.5"},
              "ABSENCE_UNINFORMED": {"leave_type": "sick"}}
    for key in keys:
        code = key.split("|")[0]
        data = {"exception_key": key, "note": "checked"}
        data.update({"action": "approve_with_value", **values[code]} if code in values else {"action": "approve"})
        client.post(url("decide"), data)
    assert "Everything is reviewed" in client.get(url("overview")).content.decode()

    r = client.post(url("finalize"), follow=True)
    assert "Payroll finalized in your copy" in r.content.decode()
    assert my_run(client, period).status == PayrollRun.FINALIZED

    client.post(url("reopen"))
    assert my_run(client, period).status == PayrollRun.DRAFT


def test_actions_require_post(client, period):
    for name in ("decide", "undo", "clear_decisions", "finalize", "reopen", "start_over"):
        assert client.get(url(name)).status_code == 405, name


def test_actions_are_csrf_protected(period):
    from django.test import Client

    strict = Client(enforce_csrf_checks=True)
    for name in ("decide", "finalize", "start_over"):
        assert strict.post(url(name)).status_code == 403, name
    assert not DemoSandbox.objects.exists()
