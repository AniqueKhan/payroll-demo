"""Pay period boundaries: a shift belongs to the day it started (its clock-in date).

Scenario 19 in the manifest: E12 works an overnight shift that starts the evening before the period
(previous payroll), and E09 works one that starts on the last day and ends the morning after (this payroll).
"""
from datetime import date, datetime
from decimal import Decimal as D

import pytest
from django.urls import reverse

from core import services
from core.models import RawImportRow

from .helpers import only, steps

FIRST, LAST = date(2026, 9, 7), date(2026, 9, 20)
BEFORE, AFTER = date(2026, 9, 6), date(2026, 9, 21)


def shift_steps(line):
    return [s for s in line.trail if s.refs.get("step") == "shift"]


def test_shift_starting_before_the_period_is_not_counted_or_flagged(result, scenario):
    s = scenario(19, employee="E12")
    assert s["date"] == BEFORE
    line = result.line_for("E12")
    assert line.hours == D("80.00") and line.gross == D("1200.00")  # 10 normal days only
    assert not [x for x in shift_steps(line) if x.refs["date"] < str(FIRST)]
    assert "22:00" not in " ".join(x.formula for x in shift_steps(line))
    assert [e.code for e in result.exceptions_for(employee_id="E12")] == ["LOAN_DEDUCTED"]


def test_shift_starting_on_the_last_day_is_fully_counted(result, scenario):
    s = scenario(19, employee="E09")
    assert s["date"] == LAST
    line = result.line_for("E09")
    last = [x for x in shift_steps(line) if x.refs["date"] == str(LAST)]
    assert [x.formula for x in last] == ["22:00-06:00 = 8.00 h"]
    # Counted in week 2 at the Store B rate; still within 40 hours (Monday was sick leave), so no overtime.
    week2_hours = sum(D(x.refs["hours"]) for x in shift_steps(line) if x.refs["date"] >= "2026-09-14")
    assert D("39.00") < week2_hours <= D("40.00")
    assert only(steps(line, "Week 2 straight time Store B")).formula.startswith(f"{week2_hours:.2f} h x $15.50")
    assert line.ot_hours == D("0.00") and line.overtime_pay == D("0.00")
    ex = only(result.exceptions_for("OVERNIGHT_SHIFT", "E09"))
    assert (ex.severity, ex.date, ex.location_code) == ("info", LAST, "B")


def test_store_b_pairs_across_both_edges(result):
    unpaired = {"ODD_PUNCH_COUNT", "MISSED_CLOCK_OUT", "MISSED_CLOCK_IN"}
    for emp in ("E09", "E12"):
        assert not [e for e in result.exceptions_for(employee_id=emp) if e.code in unpaired]
    # The only unpaired-punch problems left are the planted ones, all inside the period.
    assert sorted((e.code, e.employee_id) for e in result.exceptions if e.code in unpaired) == [
        ("MISSED_CLOCK_OUT", "E03"), ("ODD_PUNCH_COUNT", "E08")]
    assert all(FIRST <= e.date <= LAST for e in result.exceptions if e.date)


def test_last_day_overnight_adds_exactly_its_pay(result):
    # 8.00 h x $15.50 at Store B, versus the same payroll without the boundary shift.
    assert result.totals["by_location"]["B"]["gross"] == D("7069.39") + D("124.00")


@pytest.mark.django_db
def test_inputs_screen_shows_only_rows_of_this_payroll(client, tmp_path):
    period = services.seed_demo(output_dir=tmp_path)
    services.run_payroll(period)
    html = client.get(reverse("core:inputs")).content.decode()

    rows = RawImportRow.objects.filter(batch__location__code="B")
    def row(value):
        return rows.get(raw__punch_datetime=value)
    hidden = [row("09/06/2026 10:00 PM"), row("09/07/2026 06:00 AM")]  # E12, starts before the period
    shown = [row("09/20/2026 10:00 PM"), row("09/21/2026 06:00 AM")]  # E09, starts on the last day
    assert [r.in_period for r in hidden] == [False, False] and [r.in_period for r in shown] == [True, True]
    for r in hidden:
        assert f'id="row-B-{r.row_number}"' not in html
    for r in shown:
        assert f'id="row-B-{r.row_number}"' in html

    for code in ("A", "B", "C"):
        skipped = RawImportRow.objects.filter(batch__location__code=code, in_period=False).count()
        assert skipped > 0
        assert f"{skipped} rows outside Sep 7 to 20 were skipped (shifts that belong to another pay period)." in html
    # Store B skips the rows dated outside the period, except E09's last-day clock-out, plus E12's
    # first-day clock-out, which belongs to a shift that started before the period.
    def dated_outside(r):
        on = datetime.strptime(r.raw["punch_datetime"], "%m/%d/%Y %I:%M %p").date()
        return not FIRST <= on <= LAST
    expected = sum(1 for r in rows if dated_outside(r)) - 1 + 1
    assert rows.filter(in_period=False).count() == expected


def test_period_label_spans_months():
    from types import SimpleNamespace

    from core.views import period_label

    assert period_label(SimpleNamespace(start_date=date(2026, 9, 7), end_date=date(2026, 9, 20))) == "Sep 7 to 20"
    assert period_label(SimpleNamespace(start_date=date(2026, 9, 28), end_date=date(2026, 10, 11))) == \
        "Sep 28 to Oct 11"
