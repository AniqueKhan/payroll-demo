"""Overtime split at the workweek boundary (week_start_time). Scenario 21: E05's Sunday overnight shift."""
from decimal import Decimal as D

import pytest
import yaml

from core.engine.config import DEFAULT_RULES_PATH, RulesError, load_rules
from core.engine.run import run_payroll

from .helpers import only

SPLIT_LINE = ("Overnight shift Sun Sep 13 22:00 to Mon Sep 14 06:00: 2.00 h counted in Week 1, 6.00 h in Week 2. "
              "The workweek starts Monday 00:00; the split only affects overtime, the shift stays on Sun Sep 13")


def step(line, label):
    return only([s for s in line.trail if s.label == label])


@pytest.fixture(scope="module")
def week_at_six(inputs, rules):
    return run_payroll(inputs, rules.with_changes(week_start_time="06:00"))


def test_default_rules_start_the_workweek_at_midnight(rules):
    assert rules.raw["week_start_time"] == "00:00" and str(rules.week_start_time) == "00:00:00"


def test_hand_calculation_for_e05(result, scenario):
    s = scenario(21)
    assert (s["employee"], str(s["date"]), s["location"]) == ("E05", "2026-09-13", "A")
    line = result.line_for("E05")
    assert step(line, "Workweek split 2026-09-13").formula == SPLIT_LINE
    # Week 1: Mon-Fri 5 x 8.00 h + 2.00 h of the Sunday shift = 42.00 h, so 2.00 h over 40.
    assert step(line, "Week 1 hours").formula == "Store A 42.00 = 42.00"
    assert step(line, "Week 1 straight time Store A").amount == D("693.00")  # 42.00 h x $16.50
    assert step(line, "Week 1 OT premium").amount == D("16.50")  # 0.5 x $16.50 x 2.00 h
    # Week 2: Mon 8.00 + Tue 7.90 (in at 09:06, scenario 7) + Thu 8.00 + Fri 8.00 + 6.00 h = 37.90 h, no OT.
    assert step(line, "Week 2 hours").formula == "Store A 37.90 = 37.90"
    assert not [x for x in line.trail if x.label == "Week 2 OT premium"]
    assert (line.hours, line.ot_hours) == (D("79.90"), D(s["ot_hours"]))
    assert line.regular_pay == D("1318.35")  # 79.90 h x $16.50: splitting moves no hours between days
    assert (line.overtime_pay, line.gross) == (D(s["premium"]), D("1334.85"))
    # The shift itself stays on the day it started.
    assert step(line, "2026-09-13 Store A").formula == "22:00-06:00 = 8.00 h"
    assert only(result.exceptions_for("OVERNIGHT_SHIFT", "E05")).date == s["date"]


def test_week_starting_at_six_keeps_all_hours_in_week_1(week_at_six, scenario):
    s = scenario(21)
    line = week_at_six.line_for("E05")
    assert not [x for x in line.trail if x.label.startswith("Workweek split")]
    assert step(line, "Week 1 hours").formula == "Store A 48.00 = 48.00"
    assert step(line, "Week 2 hours").formula == "Store A 31.90 = 31.90"
    assert (line.ot_hours, line.overtime_pay) == (D(s["ot_hours_unsplit"]), D(s["premium_unsplit"]))  # 0.5 x 16.50 x 8


def test_only_the_boundary_shift_is_affected(result, week_at_six):
    def pay(res, employee_id):
        line = res.line_for(employee_id)
        return line.gross, line.net, line.ot_hours

    assert [x.employee_id for x in result.lines if pay(result, x.employee_id) != pay(week_at_six, x.employee_id)] \
        == ["E05"]
    # E09's overnight shift on the period's last day ends after the period: it is not split (no carryover).
    assert not [x for x in result.line_for("E09").trail if x.label.startswith("Workweek split")]


def test_split_keeps_every_hour(inputs, rules):
    # A noon week start also splits the Monday day shifts of week 2; no hour is lost or counted twice.
    res = run_payroll(inputs, rules.with_changes(week_start_time="12:00"))
    for line in res.lines:
        weeks = [x for x in line.trail if x.label.startswith("Week ") and x.label.endswith(" hours")]
        if not weeks:
            continue  # salaried: no weekly hours
        total = sum((D(x.formula.rsplit("= ", 1)[1]) for x in weeks), D(0))
        assert total == line.hours, line.employee_id
    split = step(res.line_for("E02"), "Workweek split 2026-09-14")
    assert split.formula.startswith("Shift Mon Sep 14 09:30 to Mon Sep 14 17:00: 2.50 h counted in Week 1, 5.00 h "
                                    "in Week 2.")


@pytest.mark.parametrize("value", ["24:00", "6:00", "06:60", "noon", "", 360, None])
def test_invalid_week_start_time_is_refused(rules, value):
    with pytest.raises(RulesError, match=r'week_start_time must be a time of day between "00:00" and "23:59", '
                                         r'written HH:MM in quotes \(for example "06:00"\), not '):
        rules.with_changes(week_start_time=value)


def test_unquoted_time_in_the_rules_file_is_refused(tmp_path):
    # YAML reads an unquoted 6:00 as the number 360 (base 60), so the rules file quotes it.
    text = DEFAULT_RULES_PATH.read_text().replace('week_start_time: "00:00"', "week_start_time: 6:00")
    path = tmp_path / "rules.yaml"
    path.write_text(text)
    assert yaml.safe_load(text)["week_start_time"] == 360
    with pytest.raises(RulesError, match="not 360"):
        load_rules(path)
