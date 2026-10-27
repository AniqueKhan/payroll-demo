"""Odd punch counts: a suggested pairing (breaks plus a missed clock-out); the day counts 0 h until reviewed."""
import io
from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal as D

import pytest
from django.urls import reverse

from core import services
from core.engine.hr_stage import suggest_pairing
from core.engine.run import run_payroll
from core.engine.types import Punch, ScheduledShift

from .helpers import only

E08_MESSAGE = ("3 punches: 09:00, 13:00, 13:30. Looks like a lunch break and a missed clock-out. Suggested 7.50 h, "
               "using the scheduled end of 17:00.")


def punches(*times):
    return [Punch("E08", "B", datetime.fromisoformat(f"2026-09-09T{t}"), None, n) for n, t in enumerate(times, 1)]


def schedule(start, end):
    return ScheduledShift("E08", "B", date(2026, 9, 9), datetime.fromisoformat(f"2026-09-09T{start}"),
                          datetime.fromisoformat(f"2026-09-09T{end}"))


def test_e08_message_and_suggestion(result):
    ex = only(result.exceptions_for("ODD_PUNCH_COUNT", "E08"))
    assert ex.message == E08_MESSAGE
    assert ex.context == {"suggested_hours": "7.50", "suggested_blocks": [["09:00-13:00", "4.00"],
                                                                         ["13:30-17:00", "3.50"]]}


def test_unreviewed_day_still_counts_zero_hours(result):
    line = result.line_for("E08")
    step = only([s for s in line.trail if s.label == "2026-09-09 Store B"])
    assert step.formula == ("3 punches (09:00 (row 27), 13:00 (row 31), 13:30 (row 32)): 0.00 h pending review; "
                            "suggested 7.50 h (09:00-13:00 4.00 h + 13:30-17:00 3.50 h)")
    assert step.amount is None and "hours" not in step.refs
    assert line.hours == D("72.00") and line.by_location["B"]["hours"] == D("72.00")


@pytest.mark.parametrize("times, sched, expected", [
    # 5 punches: two breaks, the last clock-in runs to the scheduled end.
    (("08:00", "10:00", "10:15", "13:00", "13:45"), ("08:00", "17:00"),
     ("5 punches: 08:00, 10:00, 10:15, 13:00, 13:45. Looks like 2 breaks and a missed clock-out. Suggested "
      "8.00 h, using the scheduled end of 17:00.",
      "8.00", [["08:00-10:00", "2.00"], ["10:15-13:00", "2.75"], ["13:45-17:00", "3.25"]])),
    # No schedule: only the paired blocks.
    (("09:00", "13:00", "13:30"), None,
     ("3 punches: 09:00, 13:00, 13:30. Looks like a lunch break and a missed clock-out. Suggested 4.00 h. There "
      "is no schedule to end the last block, so only the paired punches are counted.",
      "4.00", [["09:00-13:00", "4.00"]])),
    (("08:00", "10:00", "10:15", "13:00", "13:45"), None,
     ("5 punches: 08:00, 10:00, 10:15, 13:00, 13:45. Looks like 2 breaks and a missed clock-out. Suggested 4.75 h. "
      "There is no schedule to end the last block, so only the paired punches are counted.",
      "4.75", [["08:00-10:00", "2.00"], ["10:15-13:00", "2.75"]])),
    # The last clock-in is after the scheduled end: the schedule can't close it.
    (("09:00", "13:00", "17:30"), ("09:00", "17:00"),
     ("3 punches: 09:00, 13:00, 17:30. Looks like a lunch break and a missed clock-out. Suggested 4.00 h. The last "
      "clock-in (17:30) is after the scheduled end of 17:00, so only the paired punches are counted.",
      "4.00", [["09:00-13:00", "4.00"]])),
])
def test_suggest_pairing(times, sched, expected):
    message, context = suggest_pairing(punches(*times), schedule(*sched) if sched else None)
    assert (message, context["suggested_hours"], context["suggested_blocks"]) == expected


def test_five_punches_through_the_engine(inputs, rules, result):
    # E08 (Store B id RV-202) also clocks out for a short break at 11:00 and back in at 11:15.
    lunch = "RV-202,09/09/2026 01:00 PM,Checkout\n"
    short_break = "RV-202,09/09/2026 11:00 AM,Checkout\nRV-202,09/09/2026 11:15 AM,Checkout\n"
    text = inputs.time_files["B"].read_text().replace(lunch, short_break + lunch)
    res = run_payroll(replace(inputs, time_files={**inputs.time_files, "B": io.StringIO(text)}), rules)
    ex = only(res.exceptions_for("ODD_PUNCH_COUNT", "E08"))
    assert ex.message == ("5 punches: 09:00, 11:00, 11:15, 13:00, 13:30. Looks like 2 breaks and a missed "
                          "clock-out. Suggested 7.25 h, using the scheduled end of 17:00.")
    assert res.line_for("E08").gross == result.line_for("E08").gross  # still 0 h until reviewed


@pytest.mark.django_db
def test_screen_prefills_the_suggestion_and_approving_it_pays_the_day(client, tmp_path):
    period = services.seed_demo(output_dir=tmp_path)
    run = services.run_payroll(period)
    queue = client.get(reverse("core:exceptions")).content.decode()
    card = queue.split('id="exc-ODD_PUNCH_COUNT-E08-2026-09-09-B"')[1].split("</article>")[0]
    assert E08_MESSAGE in card
    assert 'name="hours" min="0" max="24" step="0.01" value="7.50"' in card
    assert '<span class="mono">09:00-13:00</span> (4.00 h) + <span class="mono">13:30-17:00</span> (3.50 h)' in card
    key = "ODD_PUNCH_COUNT|E08|2026-09-09|B"
    before = run.lines.get(employee__employee_id="E08").gross
    services.decide(period, key, "approve_with_value", {"hours": "7.50"})
    after = services.current_run(period).lines.get(employee__employee_id="E08").gross
    assert after - before == D("112.50")  # 7.50 h x $15.00
