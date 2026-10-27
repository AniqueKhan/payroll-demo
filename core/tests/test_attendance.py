"""Net before taxes, attendance items to discuss, and the attendance penalties setting."""
import io
from dataclasses import replace
from decimal import Decimal as D
from io import StringIO

import pytest
from django.core.management import call_command
from django.urls import reverse

from core import services
from core.engine.run import run_payroll

from .helpers import only

TAX_NOTE = ("Federal, state and payroll tax withholding is calculated by your payroll provider (for example ADP, "
            "Gusto or Paychex). This system prepares checked, ready-to-run numbers for it.")
ATTENDANCE_INTRO = "These don&#x27;t change anyone&#x27;s pay. They&#x27;re here so you can follow up with the employee."


@pytest.fixture(scope="module")
def penalties_on(inputs, rules):
    return run_payroll(inputs, rules.with_changes(attendance_penalties_enabled=True))


# ------------------------------------------------------------------ engine

def test_attendance_items_have_their_own_group(result):
    counts = result.totals["exceptions_by_severity"]
    assert counts == {"needs_review": 6, "info": 14, "attendance": 2}
    assert sorted(e.code for e in result.exceptions if e.severity == "attendance") == \
        ["ABSENCE_UNINFORMED", "LATE_ARRIVAL"]


def test_uninformed_absence_still_deducts_the_day(result):
    ex = only(result.exceptions_for("ABSENCE_UNINFORMED", "E07"))
    assert ex.message == ("E07 Harper Lane: absent Thu Sep 17 with no notice and no leave record. "
                          "One day's pay deducted ($220.00).")
    assert ex.context["occurrence_label"] == "1st absence without notice this period"
    line = result.line_for("E07")
    assert (line.absence_deductions, line.penalties) == (D("220.00"), D("0.00"))


def test_penalties_off_by_default_raise_net_by_the_penalty(result, penalties_on):
    for employee, penalty in (("E07", D("25.00")), ("E02", D("5.00"))):
        off, on = result.line_for(employee), penalties_on.line_for(employee)
        assert (off.penalties, on.penalties) == (D("0.00"), penalty)
        assert off.net - on.net == penalty
        assert off.gross == on.gross
    assert (result.line_for("E07").net, result.line_for("E02").net) == (D("1890.90"), D("1221.12"))
    assert result.totals["net"] - penalties_on.totals["net"] == D("30.00")


def test_penalties_on_reproduce_the_previous_numbers(penalties_on):
    totals = penalties_on.totals
    assert (totals["gross"], totals["net"], totals["penalties"]) == (D("24341.99"), D("22684.91"), D("30.00"))
    assert (penalties_on.line_for("E07").net, penalties_on.line_for("E02").net) == (D("1865.90"), D("1216.12"))
    assert only(penalties_on.exceptions_for("LATE_ARRIVAL", "E02")).message.endswith("Late penalty $5.00.")
    assert only(penalties_on.exceptions_for("ABSENCE_UNINFORMED", "E07")).message.endswith(
        "One day's pay deducted ($220.00). Absence penalty $25.00.")
    # Penalties never move items out of the attendance group.
    assert totals["exceptions_by_severity"] == {"needs_review": 6, "info": 14, "attendance": 2}


def test_running_count_with_two_late_arrivals(inputs, rules):
    # E02 (Store A id 1002) also clocks in at 09:20 on Mon Sep 7, a week before the planted late arrival.
    text = inputs.time_files["A"].read_text().replace("1002,2026-09-07,09:00,IN", "1002,2026-09-07,09:20,IN")
    res = run_payroll(replace(inputs, time_files={**inputs.time_files, "A": io.StringIO(text)}), rules)
    lates = res.exceptions_for("LATE_ARRIVAL", "E02")
    assert [(str(e.date), e.context["occurrence"], e.context["occurrence_label"]) for e in lates] == [
        ("2026-09-07", 1, "1st late arrival this period"),
        ("2026-09-14", 2, "2nd late arrival this period"),
    ]
    assert res.line_for("E02").penalties == D("0.00")


# ------------------------------------------------------------------ screens, services and the CLI

@pytest.fixture
def period(db, tmp_path):
    period = services.seed_demo(output_dir=tmp_path)
    services.run_payroll(period)
    return period


def test_net_before_taxes_on_every_screen(client, period):
    for name, kw in (("overview", {}), ("payroll", {}), ("employee", {"employee_id": "E07"})):
        html = client.get(reverse(f"core:{name}", kwargs=kw)).content.decode()
        assert "Net before taxes" in html or "net before taxes" in html, name
        assert ">Net<" not in html and "Net pay" not in html, name
    for name in ("overview", "payroll"):
        assert TAX_NOTE in client.get(reverse(f"core:{name}")).content.decode()
    employee = client.get(reverse("core:employee", kwargs={"employee_id": "E07"})).content.decode()
    trail = employee.split("Calculation trail")[1].split("By store and department")[0]
    assert "Net before taxes" in trail  # the trail's last step


def test_net_before_taxes_in_the_cli(period):
    out = StringIO()
    call_command("run_payroll", "--period", "2026-09-07", stdout=out)
    text = out.getvalue()
    assert "Net before taxes:" in text and "Net pay" not in text
    assert "1st late arrival this period" in text


def test_attendance_items_are_counted_apart_and_shown_below_pay_items(client, period):
    overview = client.get(reverse("core:overview")).content.decode()
    assert "2 attendance items to discuss" in overview
    assert "6 items need your review" in overview
    assert '<span class="pill pill-info">Handled by rule</span> <strong>14</strong>' in overview
    queue = client.get(reverse("core:exceptions")).content.decode()
    assert ATTENDANCE_INTRO in queue
    section = queue.split('id="attendance"')[1]
    assert "E07 Harper Lane: absent Thu Sep 17" in section and "1st late arrival this period" in section
    assert queue.index('id="attendance"') > queue.index('id="info"')
    assert 'value="approve"' not in section  # nothing to decide


def test_attendance_items_never_block_finalizing(period):
    run = services.current_run(period)
    for key in run.exceptions.filter(severity="needs_review").values_list("exception_key", flat=True):
        services.decide(period, key, "approve")
    run = services.current_run(period)
    assert run.exceptions.filter(severity="attendance").count() == 2
    assert services.finalize(period).status == "finalized"


# ------------------------------------------------------------------ optional "Record as leave" on absences

E07_KEY = "ABSENCE_UNINFORMED|E07|2026-09-17|B"


def e07(period, sandbox=None):
    return services.current_run(period, sandbox).lines.get(employee__employee_id="E07")


@pytest.mark.parametrize("leave, deduction", [("paid", D("0.00")), ("unpaid", D("220.00"))])
def test_record_absence_as_leave(period, leave, deduction):
    before = e07(period)
    services.decide(period, E07_KEY, "approve_with_value", {"leave": leave}, "called the store")
    after = e07(period)
    assert after.absence_deductions == deduction
    assert after.gross - before.gross == D("220.00") - deduction
    step = only([s for s in after.trail if s["label"] == "Reviewer adjustment: recorded as leave"])
    assert step["formula"].startswith(f"2026-09-17: absence without notice recorded as {leave} leave")
    assert step["formula"].endswith("(note: called the store)")
    record = services.current_run(period).exceptions.get(exception_key=E07_KEY)
    assert (record.severity, record.status, record.adjusted) == ("attendance", "approved", True)
    assert record.resolution.startswith(f"Recorded as {leave} leave by a reviewer")
    # The decision survives re-runs, and undo puts the deduction back.
    services.run_payroll(period)
    assert e07(period).absence_deductions == deduction
    services.undo_decision(period, E07_KEY)
    assert e07(period).absence_deductions == D("220.00")


def test_record_as_leave_is_never_required_to_finalize(period):
    services.decide(period, E07_KEY, "approve_with_value", {"leave": "paid"})
    with pytest.raises(services.FinalizeRefused):
        services.finalize(period)  # the 6 needs_review items are still open; the leave choice doesn't count
    for key in services.current_run(period).exceptions.filter(severity="needs_review") \
            .values_list("exception_key", flat=True):
        services.decide(period, key, "approve")
    assert services.finalize(period).status == "finalized"


def test_record_as_leave_on_screen_is_per_visitor(client, period):
    from django.test import Client

    queue = client.get(reverse("core:exceptions")).content.decode()
    form = queue.split('id="exc-ABSENCE_UNINFORMED-E07-2026-09-17-B"')[1].split("</article>")[0]
    assert "Record as leave" in form and 'name="leave"' in form and "never blocks finalizing" in form
    late = queue.split('id="exc-LATE_ARRIVAL-E02-2026-09-14-A"')[1].split("</article>")[0]
    assert "<form" not in late

    r = client.post(reverse("core:decide"), {"exception_key": E07_KEY, "action": "approve_with_value",
                                             "leave": "paid"}, follow=True)
    html = r.content.decode()
    assert "Recorded as paid leave: Absence without notice. 6 items still need review." in html
    assert "Recorded as paid leave</strong>" in html and "Undo" in html
    employee = client.get(reverse("core:employee", kwargs={"employee_id": "E07"})).content.decode()
    assert "absence without notice recorded as paid leave" in employee
    # Another visitor and the baseline still see the deduction.
    other = Client(HTTP_HOST="localhost")
    assert "Record as leave" in other.get(reverse("core:exceptions")).content.decode()
    assert e07(period).absence_deductions == D("220.00")
