"""The demo can be generated for any pay period that starts on the rules' week start day.

Only the dates move: every period produces the same exceptions, employee totals, gross and net.
"""
from collections import Counter
from datetime import date, timedelta
from io import StringIO

import pytest
from django.core.management import CommandError, call_command

from core.demo_data.generator import PeriodStartError, generate
from core.engine.inputs import load_inputs_from_dir, read_manifest
from core.engine.run import run_payroll
from core.models import Employee, PayPeriod, PayrollRun

PERIODS = [date(2026, 9, 7), date(2026, 10, 5), date(2027, 1, 4)]
LINE_FIELDS = ("regular_pay", "overtime_pay", "addons", "penalties", "absence_deductions", "loan_deductions",
               "custom_deductions", "gross", "net", "hours", "ot_hours")


@pytest.fixture(scope="module")
def runs(tmp_path_factory, rules):
    """Engine results (no database) for each period."""
    out = {}
    for start in PERIODS:
        folder = tmp_path_factory.mktemp(f"period-{start}")
        generate(folder, period_start=start, rules=rules)
        out[start] = (read_manifest(folder), run_payroll(load_inputs_from_dir(folder, rules), rules))
    return out


def outcome(result):
    return {
        "exceptions": Counter((e.code, e.severity) for e in result.exceptions),
        "employees": {line.employee_id: tuple(getattr(line, f) for f in LINE_FIELDS) for line in result.lines},
        "gross": result.totals["gross"],
        "net": result.totals["net"],
        "by_location": result.totals["by_location"],
    }


@pytest.mark.parametrize("start", PERIODS[1:])
def test_every_period_produces_the_same_payroll(runs, start):
    _, baseline = runs[PERIODS[0]]
    _, other = runs[start]
    assert other.period.start == start and other.period.end == start + timedelta(days=13)
    assert outcome(other) == outcome(baseline)


@pytest.mark.parametrize("start", PERIODS)
def test_planted_scenarios_move_with_the_period(runs, start):
    manifest, result = runs[start]
    assert (manifest["period_start"], manifest["period_end"]) == (start.isoformat(),
                                                                  (start + timedelta(days=13)).isoformat())
    for number, offset, code, employee in [("4", 3, "MISSED_CLOCK_OUT", "E03"), ("6", 4, "OVERNIGHT_SHIFT", "E04"),
                                           ("9", 10, "ABSENCE_UNINFORMED", "E07"), ("16", 4, "EXIT_FINAL_SETTLEMENT",
                                                                                     "E13")]:
        entry = next(e for e in manifest["scenarios"][number] if e["code"] == code)
        expected = start + timedelta(days=offset)
        assert entry["date"] == expected.isoformat()
        assert expected.weekday() == date(2026, 9, 7 + offset).weekday()  # same weekday as the default period
        assert [e.date for e in result.exceptions_for(code, employee)] == [expected]


def test_default_period_is_unchanged(runs):
    manifest, result = runs[PERIODS[0]]
    assert manifest["period_start"] == "2026-09-07"
    assert str(result.totals["gross"]) == "24466.49" and str(result.totals["net"]) == "22833.92"


def test_non_week_start_date_is_refused_with_a_clear_message(tmp_path, rules):
    with pytest.raises(PeriodStartError) as refused:
        generate(tmp_path / "out", period_start=date(2026, 10, 8), rules=rules)
    assert str(refused.value) == ("2026-10-08 is a Thursday. Pay periods in the demo rules start on Monday. "
                                  "Try 2026-10-05 or 2026-10-12.")
    assert not (tmp_path / "out").exists()  # nothing written


@pytest.mark.django_db
def test_seed_demo_command_refuses_a_non_monday_and_seeds_nothing(tmp_path):
    with pytest.raises(CommandError, match="2026-10-08 is a Thursday. Pay periods in the demo rules start on "
                                           "Monday. Try 2026-10-05 or 2026-10-12."):
        call_command("seed_demo", "--period", "2026-10-08", "--output", str(tmp_path / "out"), stdout=StringIO())
    assert not PayPeriod.objects.exists() and not Employee.objects.exists()
    assert not (tmp_path / "out").exists()


@pytest.mark.django_db
def test_seed_demo_command_refuses_a_bad_date(tmp_path):
    with pytest.raises(CommandError, match="--period must be a date"):
        call_command("seed_demo", "--period", "next monday", "--output", str(tmp_path), stdout=StringIO())


@pytest.mark.django_db
def test_seed_and_run_another_period_through_the_database(tmp_path):
    call_command("seed_demo", "--period", "2027-01-04", "--output", str(tmp_path), stdout=StringIO())
    period = PayPeriod.objects.get()
    assert (period.start_date, period.end_date) == (date(2027, 1, 4), date(2027, 1, 17))
    assert Employee.objects.get(employee_id="E14").hire_date == date(2027, 1, 6)
    assert Employee.objects.get(employee_id="E13").exit_date == date(2027, 1, 8)
    call_command("run_payroll", "--period", "2027-01-04", stdout=StringIO())
    run = PayrollRun.objects.get()
    assert (run.totals["gross"], run.totals["net"]) == ("24466.49", "22833.92")
    assert run.exceptions.filter(severity="needs_review", status="open").count() == 6
