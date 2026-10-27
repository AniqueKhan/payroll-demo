"""Wrong location: paid where the employee clocked in by default; the reviewer can say it was the scheduled store."""
from dataclasses import replace
from datetime import date
from decimal import Decimal as D

import pytest
from django.test import Client
from django.urls import reverse

from core import services
from core.engine.run import run_payroll
from core.engine.types import Adjustments, LocationOverride

from .helpers import only

KEY = "WRONG_LOCATION|E10|2026-09-16|B"
AT_A = "Worked at Store A (where they clocked in)"
AT_B = "Worked at Store B (scheduled), clocked in on the wrong device"


@pytest.fixture(scope="module")
def at_b(inputs, rules):
    return run_payroll(inputs, rules, Adjustments(location_overrides=[
        LocationOverride("E10", date(2026, 9, 16), "B", "badge reader at A")]))


def shift_step(line):
    return only([s for s in line.trail if s.label.startswith("2026-09-16 ")])


# ------------------------------------------------------------------ engine

def test_default_pays_the_store_where_they_clocked_in(result):
    ex = only(result.exceptions_for("WRONG_LOCATION", "E10"))
    assert ex.message == ("Scheduled at Store B ($16.00/h) but clocked in at Store A ($17.00/h). Paid at Store A's "
                          "rate by default. Difference for this shift: $8.00.")
    assert ex.context["default"] == "A"
    assert ex.context["choices"] == [["A", AT_A], ["B", AT_B]]
    line = result.line_for("E10")
    assert line.by_location["A"] == {"hours": D("8.00"), "regular_pay": D("136.00"), "overtime_pay": D("0.00"),
                                     "addons": D("0.00"), "absence_deductions": D("0.00"), "gross": D("136.00")}
    step = shift_step(line)
    assert step.label == "2026-09-16 Store A"
    assert step.formula == "09:00-17:00 = 8.00 h (scheduled elsewhere; paid where they clocked in, pending review)"


def test_choosing_the_scheduled_store_moves_rate_store_and_department_totals(result, at_b):
    default, chosen = result.line_for("E10"), at_b.line_for("E10")
    assert default.gross - chosen.gross == D("8.00")  # 8 h x ($17.00 - $16.00)
    assert set(chosen.by_location) == {"B"} and chosen.by_location["B"]["regular_pay"] == D("1280.00")
    assert shift_step(chosen).label == "2026-09-16 Store B"
    by_store = lambda res, c: res.totals["by_location"][c]  # noqa: E731
    assert by_store(result, "A")["gross"] - by_store(at_b, "A")["gross"] == D("136.00")  # 8 h x $17.00
    assert by_store(at_b, "B")["gross"] - by_store(result, "B")["gross"] == D("128.00")  # 8 h x $16.00
    assert by_store(result, "A")["hours"] - by_store(at_b, "A")["hours"] == D("8.00")
    floor = lambda res, c: res.totals["by_location_department"][c]["Floor"]["gross"]  # noqa: E731
    assert floor(result, "A") - floor(at_b, "A") == D("136.00")
    assert floor(at_b, "B") - floor(result, "B") == D("128.00")
    assert result.totals["by_department"]["Floor"]["gross"] - at_b.totals["by_department"]["Floor"]["gross"] == \
        D("8.00")


def test_trail_shows_the_reviewers_choice(at_b):
    line = at_b.line_for("E10")
    step = only([s for s in line.trail if s.label == "Reviewer adjustment: pay store"])
    assert step.formula == (f'2026-09-16: reviewer chose "{AT_B}": paid at Store B\'s rate ($16.00/h), cost '
                            "allocated to Store B (note: badge reader at A)")
    ex = only(at_b.exceptions_for("WRONG_LOCATION", "E10"))
    assert ex.adjusted and ex.resolution == f"Reviewer chose: {AT_B}. Note: badge reader at A"


def test_no_rate_at_the_punch_store_falls_back_to_the_scheduled_store(inputs, rules):
    employees = [replace(e, rates={"B": e.rates["B"]}) if e.id == "E10" else e for e in inputs.employees]
    res = run_payroll(replace(inputs, employees=employees), rules)
    ex = only(res.exceptions_for("WRONG_LOCATION", "E10"))
    assert ex.message == ("Scheduled at Store B but clocked in at Store A, where there is no pay rate. Paid at "
                          "Store B's rate by default.")
    assert (ex.context["default"], ex.context["choices"]) == ("B", [["B", AT_B]])
    assert set(res.line_for("E10").by_location) == {"B"}


# ------------------------------------------------------------------ decisions and screens

@pytest.fixture
def period(db, tmp_path):
    period = services.seed_demo(output_dir=tmp_path)
    services.run_payroll(period)
    return period


def e10(period, sandbox=None):
    return services.current_run(period, sandbox).lines.get(employee__employee_id="E10")


def test_choice_is_a_decision_that_survives_recalculation(period):
    before = e10(period)
    services.decide(period, KEY, "approve_with_value", {"location": "B"}, "wrong device")
    for _ in range(2):
        services.run_payroll(period)
        line = e10(period)
        assert before.gross - line.gross == D("8.00")
        assert set(line.by_location) == {"B"}
        assert any(f'reviewer chose "{AT_B}"' in s["formula"] for s in line.trail)
    record = services.current_run(period).exceptions.get(exception_key=KEY)
    assert (record.status, record.adjusted) == ("approved", True)
    services.undo_decision(period, KEY)
    assert e10(period).gross == before.gross


def test_approving_as_is_records_the_default_store(period):
    services.decide(period, KEY, "approve")
    decision = period.decisions.get(exception_key=KEY)
    assert (decision.action, decision.value) == ("approve_with_value", {"location": "A"})
    line = e10(period)
    assert line.by_location["A"]["regular_pay"] == "136.00"
    assert any(f'reviewer chose "{AT_A}"' in s["formula"] for s in line.trail)


def test_a_store_outside_the_choices_is_refused(period):
    with pytest.raises(services.DecisionError, match="no pay rate"):
        services.decide(period, KEY, "approve_with_value", {"location": "C"})


def test_screen_offers_the_two_choices_per_visitor(client, period):
    queue = client.get(reverse("core:exceptions")).content.decode()
    card = queue.split('id="exc-WRONG_LOCATION-E10-2026-09-16-B"')[1].split("</article>")[0]
    assert "Where was this shift worked?" in card
    assert '<input type="radio" name="location" value="A" checked>' in card and AT_A in card and AT_B in card
    assert "Approve as is" not in card and "Confirm where it was worked" in card
    client.post(reverse("core:decide"), {"exception_key": KEY, "action": "approve_with_value", "location": "B"})
    queue = client.get(reverse("core:exceptions")).content.decode()
    card = queue.split('id="exc-WRONG_LOCATION-E10-2026-09-16-B"')[1].split("</article>")[0]
    decision = card.split('<div class="decision">')[1]
    assert "Approved with value" in decision and AT_B in decision and AT_A not in decision
    employee = client.get(reverse("core:employee", kwargs={"employee_id": "E10"})).content.decode()
    assert "Reviewer adjustment: pay store" in employee
    # Another visitor and the baseline keep the default.
    assert "Reviewer adjustment: pay store" not in \
        Client(HTTP_HOST="localhost").get(reverse("core:employee", kwargs={"employee_id": "E10"})).content.decode()
    assert e10(period).by_location["A"]["regular_pay"] == "136.00"
