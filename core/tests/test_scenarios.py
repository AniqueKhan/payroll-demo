"""One test per scenario, located through scenario_manifest.json.

Engine tests use no database. Scenario 17 (review gate) is a service-level test and does.
"""
import io
from dataclasses import replace
from datetime import date
from decimal import Decimal as D

import pytest

from core.demo_data.generator import generate
from core.engine.importers import StoreAImporter, StoreBImporter, StoreCImporter
from core.engine.inputs import load_inputs_from_dir
from core.engine.run import run_payroll

from .helpers import only, steps


def shift_hours(line, day):
    return [D(s.refs["hours"]) for s in line.trail if s.refs.get("step") == "shift" and s.refs["date"] == str(day)
            and "hours" in s.refs]


# 1 -------------------------------------------------------------------------------------------

def test_01_importers_normalize_each_format(inputs):
    a, _ = StoreAImporter("A", inputs.employees).parse(io.StringIO(
        "EmpID,Date,Time,Type\n1002,2026-09-07,09:00,IN\n1002,2026-09-07,17:00,OUT\n"))
    b, _ = StoreBImporter("B", inputs.employees).parse(io.StringIO(
        "employee_code,punch_datetime\nRV-202,09/07/2026 09:00 AM\nRV-202,09/07/2026 05:00 PM\n"))
    c, _ = StoreCImporter("C", inputs.employees).parse(io.StringIO(
        "Name,Date,In,Out\n  sage   DELGADO ,2026-09-07,22:00,06:00\n"))
    assert [(p.employee_id, p.kind, p.timestamp.hour) for p in a] == [("E02", "in", 9), ("E02", "out", 17)]
    assert [(p.employee_id, p.kind, p.timestamp.hour) for p in b] == [("E08", None, 9), ("E08", None, 17)]
    assert [(p.employee_id, p.kind, str(p.timestamp)) for p in c] == [
        ("E16", "in", "2026-09-07 22:00:00"), ("E16", "out", "2026-09-08 06:00:00")]


def test_01_unknown_column_is_info(result, scenario):
    col = only(result.exceptions_for("UNKNOWN_COLUMN"))
    assert (col.severity, col.location_code) == ("info", scenario(1, code="UNKNOWN_COLUMN")["location"])
    assert "'Dept'" in col.message


def test_01_malformed_row_holds_the_day_for_review(result, scenario):
    # Row 'E02 2026-09-09 9:6O IN' fails to parse. The day's other punches (09:00 IN, 17:00 OUT)
    # would pair into 8 h, but without knowing what the bad punch was, that pay would be a guess.
    s = scenario(1, code="MALFORMED_ROW", severity="needs_review")
    bad = only(result.exceptions_for("MALFORMED_ROW", s["employee"]))
    assert (bad.severity, bad.auto_resolved, bad.date, bad.location_code) == (
        "needs_review", False, s["date"], s["location"])
    assert "9:6O" in bad.message
    assert "Remaining punches: 09:00 IN (row" in bad.message and "17:00 OUT (row" in bad.message
    assert f"suggested {s['scheduled_hours']} h" in bad.message

    line = result.line_for(s["employee"])
    assert shift_hours(line, s["date"]) == []  # not paid from the remaining punches
    assert line.regular_pay == D("1144.00")  # 71.50 h x $16.00: 9 Sep held at 0 h
    assert not [e for e in result.exceptions_for(employee_id=s["employee"])
                if e.date == s["date"] and e.code != "MALFORMED_ROW"]  # no late/absence invented


def test_01_malformed_row_outside_period_stays_info(result, scenario):
    s = scenario(1, code="MALFORMED_ROW", severity="info")
    bad = only(result.exceptions_for("MALFORMED_ROW", s["employee"]))
    assert (bad.severity, bad.auto_resolved, bad.date) == ("info", True, s["date"])
    assert "outside the period" in bad.resolution
    assert result.line_for(s["employee"]).regular_pay == D("1116.00")  # unchanged, see scenario 4


def test_01_malformed_row_with_unreadable_date_needs_review(inputs, rules):
    store_a = inputs.time_files["A"].read_text() + "1005,2026-13-40,09:00,IN\n"
    res = run_payroll(replace(inputs, time_files={**inputs.time_files, "A": io.StringIO(store_a)}), rules)
    bad = only(res.exceptions_for("MALFORMED_ROW", "E05"))
    assert (bad.severity, bad.date) == ("needs_review", None)
    assert "Work date unreadable" in bad.message


def test_01_store_b_malformed_row_still_locates_the_day(inputs):
    _, found = StoreBImporter("B", inputs.employees).parse(io.StringIO(
        "employee_code,punch_datetime\nRV-202,09/09/2026 9:6O AM\nRV-202,\n"))
    assert [(e.code, e.employee_id, e.date) for e in found] == [
        ("MALFORMED_ROW", "E08", date(2026, 9, 9)), ("MALFORMED_ROW", "E08", None)]


def test_01_unreadable_file_is_blocking_not_a_crash(inputs, rules):
    punches, found = StoreAImporter("A", inputs.employees).parse(io.StringIO("Employee;When\n1002;whenever\n"))
    assert punches == []
    assert [(e.code, e.severity) for e in found] == [("FILE_UNREADABLE", "blocking")]

    _, found = StoreBImporter("B", inputs.employees).parse("/nonexistent/store_b.csv")
    assert [(e.code, e.severity) for e in found] == [("FILE_UNREADABLE", "blocking")]

    broken = replace(inputs, time_files={**inputs.time_files, "B": io.StringIO("garbage\n\x00\x00")})
    res = run_payroll(broken, rules)
    assert only(res.exceptions_for("FILE_UNREADABLE")).location_code == "B"
    # Store B data is treated as missing: no absences invented for its schedules.
    assert not [e for e in res.exceptions_for("ABSENCE_UNINFORMED") if e.location_code == "B"]


# 2 -------------------------------------------------------------------------------------------

def test_02_missing_store_file_is_blocking_and_nothing_is_estimated(tmp_path, rules):
    manifest = generate(tmp_path, missing_store="B")
    s = manifest["scenarios"]["2"][0]
    res = run_payroll(load_inputs_from_dir(tmp_path, rules), rules)
    missing = only(res.exceptions_for("STORE_FILE_MISSING"))
    assert (missing.severity, missing.location_code) == ("blocking", s["location"])
    # Nothing is filled in: a store-B-only hourly employee has no hours and no invented absences.
    nico = res.line_for("E12")
    assert nico.regular_pay == D("0.00") and nico.by_location == {}
    assert not [e for e in res.exceptions if e.location_code == "B" and e.code.startswith("ABSENCE")]
    assert nico.custom_deductions == D("0.00") and nico.net == D("0.00")


# 3 -------------------------------------------------------------------------------------------

def test_03_duplicate_punch_keeps_first(result, scenario):
    s = scenario(3)
    dup = only(result.exceptions_for("DUPLICATE_PUNCH", s["employee"]))
    assert (dup.severity, dup.date, dup.location_code) == ("info", s["date"], "A")
    assert "Kept first punch" in dup.resolution
    line = result.line_for(s["employee"])
    assert shift_hours(line, s["date"]) == [D("8.00")]


# 4 -------------------------------------------------------------------------------------------

def test_04_missed_clock_out_counts_zero_and_suggests_schedule(result, scenario):
    s = scenario(4)
    ex = only(result.exceptions_for("MISSED_CLOCK_OUT", s["employee"]))
    assert (ex.severity, ex.date) == ("needs_review", s["date"])
    assert f"suggested {s['scheduled_hours']} h" in ex.message
    line = result.line_for(s["employee"])
    assert line.regular_pay == D("1116.00")  # 72 h paid x $15.50, the open shift counts 0 h
    assert not result.exceptions_for("ABSENCE_UNINFORMED", s["employee"])


# 5 -------------------------------------------------------------------------------------------

def test_05_odd_punches_counted_zero_with_raw_punches(result, scenario):
    s = scenario(5)
    ex = only(result.exceptions_for("ODD_PUNCH_COUNT", s["employee"]))
    assert (ex.severity, ex.date, ex.location_code) == ("needs_review", s["date"], "B")
    for raw in ("09:00", "13:00", "13:30"):
        assert raw in ex.message
    assert result.line_for(s["employee"]).regular_pay == D("1080.00")  # 72 h x $15.00


# 6 -------------------------------------------------------------------------------------------

def test_06_overnight_shift_is_one_shift_on_start_date(result, scenario):
    s = scenario(6)
    ex = only(result.exceptions_for("OVERNIGHT_SHIFT", s["employee"]))
    assert (ex.severity, ex.date) == ("info", s["date"])
    line = result.line_for(s["employee"])
    assert shift_hours(line, s["date"]) == [D(s["hours"])]
    assert shift_hours(line, date(2026, 9, 12)) == []
    assert line.regular_pay == D("1360.00") and line.overtime_pay == D("0.00")
    assert not [e for e in result.exceptions if e.employee_id == s["employee"] and e.code != "OVERNIGHT_SHIFT"]


# 7 -------------------------------------------------------------------------------------------

def test_07_late_beyond_grace_is_penalized(result, scenario):
    s = scenario(7, code="LATE_ARRIVAL")
    ex = only(result.exceptions_for("LATE_ARRIVAL", s["employee"]))
    assert (ex.severity, ex.date) == ("info", s["date"])
    assert f"{s['minutes_late']} min late" in ex.message
    assert result.line_for(s["employee"]).penalties == D(s["penalty"])


def test_07_late_within_grace_is_not_penalized(result, scenario):
    s = scenario(7, code=None)
    assert not result.exceptions_for("LATE_ARRIVAL", s["employee"])
    line = result.line_for(s["employee"])
    assert line.penalties == D("0.00")
    assert shift_hours(line, s["date"]) == [D("7.90")]


# 8 -------------------------------------------------------------------------------------------

def test_08_half_day_deducts_half_daily_rate(result, scenario):
    s = scenario(8)
    ex = only(result.exceptions_for("HALF_DAY", s["employee"]))
    assert (ex.severity, ex.date) == ("info", s["date"])
    line = result.line_for(s["employee"])
    assert line.absence_deductions == D(s["deduction"])
    assert line.gross == D("1900.00")


# 9 -------------------------------------------------------------------------------------------

def test_09_uninformed_absence(result, scenario):
    s = scenario(9, code="ABSENCE_UNINFORMED")
    ex = only(result.exceptions_for("ABSENCE_UNINFORMED", s["employee"]))
    assert (ex.severity, ex.date) == (s["severity"], s["date"])
    line = result.line_for(s["employee"])
    assert line.absence_deductions == D(s["deduction"])
    assert line.penalties == D(s["penalty"])


def test_09_informed_paid_leave(result, scenario):
    s = scenario(9, leave_type="sick")
    ex = only(result.exceptions_for("ABSENCE_INFORMED", s["employee"]))
    assert (ex.severity, ex.date) == (s["severity"], s["date"])
    leave = only(steps(result.line_for(s["employee"]), f"Paid leave {s['date']}"))
    assert leave.amount == D(s["leave_pay"])
    assert not result.exceptions_for("ABSENCE_UNINFORMED", s["employee"])


def test_09_informed_unpaid_leave(result, scenario):
    s = scenario(9, leave_type="unpaid")
    ex = only(result.exceptions_for("ABSENCE_INFORMED", s["employee"]))
    assert (ex.severity, ex.date) == (s["severity"], s["date"])
    line = result.line_for(s["employee"])
    assert not steps(line, f"Paid leave {s['date']}") and line.penalties == D("0.00")


# 10 ------------------------------------------------------------------------------------------

def test_10_hours_summed_across_stores_before_overtime(result, scenario):
    s = scenario(10)
    ex = only(result.exceptions_for("MULTI_LOCATION_OVERTIME", s["employee"]))
    assert ex.severity == "info"
    line = result.line_for(s["employee"])
    hours = only(steps(line, f"Week {s['week']} hours"))
    assert hours.formula == "Store A 32.00 + Store B 12.00 = 44.00"
    assert line.overtime_pay == D(s["premium"])
    assert {k: str(v["hours"]) for k, v in line.by_location.items()} == {"A": "72.00", "B": "12.00"}


# 11 ------------------------------------------------------------------------------------------

def test_11_wrong_store_paid_and_allocated_to_scheduled_store(result, scenario):
    s = scenario(11)
    ex = only(result.exceptions_for("WRONG_LOCATION", s["employee"]))
    assert (ex.severity, ex.date, ex.location_code) == ("needs_review", s["date"], s["location"])
    line = result.line_for(s["employee"])
    assert line.regular_pay == D("1280.00")  # 80 h at the store B rate ($16), not store A ($17)
    assert set(line.by_location) == {"B"}
    assert line.by_location["B"]["regular_pay"] == D("1280.00")


# 12 ------------------------------------------------------------------------------------------

def test_12_ot_premium_on_weighted_average_rate(result, scenario):
    s = scenario(12)
    ex = only(result.exceptions_for("BLENDED_RATE_OVERTIME", s["employee"]))
    assert ex.severity == "info"
    line = result.line_for(s["employee"])
    assert only(steps(line, "Week 2 straight time Store B")).amount == D("216.00")  # 12 h x $18
    assert only(steps(line, "Week 2 straight time Store A")).amount == D("512.00")  # 32 h x $16
    # The weighted rate keeps full precision: 0.5 x (728 / 44) x 4 = 33.0909 -> $33.09.
    # Rounding the rate to $16.55 first would give $33.10.
    rate = only(steps(line, f"Week {s['week']} weighted rate"))
    assert rate.amount == D(s["weighted_rate"])  # shown to cents
    assert D(rate.refs["rate"]) == D("728.00") / D("44.00")  # used unrounded
    premium = only(steps(line, f"Week {s['week']} OT premium"))
    assert premium.formula == f"0.5 x ($728.00 / 44.00 h) x {s['ot_hours']} h OT = ${s['premium']}"
    assert premium.amount == D("33.09")
    assert line.overtime_pay == D("33.09")
    # Premium is allocated to stores by hours worked that week.
    assert line.by_location["A"]["overtime_pay"] + line.by_location["B"]["overtime_pay"] == D(s["premium"])


# 13 ------------------------------------------------------------------------------------------

def test_13_advance_within_cap(result, scenario):
    s = scenario(13, code="LOAN_DEDUCTED")
    ex = only(result.exceptions_for("LOAN_DEDUCTED", s["employee"]))
    assert ex.severity == s["severity"]
    line = result.line_for(s["employee"])
    assert line.loan_deductions == D(s["deducted"])
    step = only(steps(line, "Advance installment"))
    assert step.formula == f"${s['deducted']} (balance after: ${s['balance_after']})"


def test_13_loan_over_cap_carries_remainder(result, scenario):
    s = scenario(13, code="LOAN_CAPPED")
    ex = only(result.exceptions_for("LOAN_CAPPED", s["employee"]))
    assert ex.severity == s["severity"]
    assert f"${s['carried']} carried" in ex.message
    assert result.line_for(s["employee"]).loan_deductions == D(s["deducted"])


# 14 ------------------------------------------------------------------------------------------

def test_14_commission_in_regular_rate_recalculates_ot(result, scenario):
    s = scenario(14)
    ex = only(result.exceptions_for("ADDON_OT_RECALC", s["employee"]))
    assert ex.severity == "info"
    assert f"${s['premium_before']} -> ${s['premium_after']}" in ex.message
    line = result.line_for(s["employee"])
    assert line.addons == D("90.00")
    assert line.overtime_pay == D(s["premium_after"])
    assert line.gross == D("1407.50")  # 1275.00 straight + 42.50 OT + 90.00 commission


def test_14_bonus_outside_regular_rate_just_adds_to_gross(result):
    line = result.line_for("E16")
    assert line.addons == D("100.00") and line.gross == D("1380.00")
    assert not result.exceptions_for("ADDON_OT_RECALC", "E16")


# 15 ------------------------------------------------------------------------------------------

def test_15_new_hire_prorated(result, scenario):
    s = scenario(15)
    ex = only(result.exceptions_for("NEW_HIRE_PRORATED", s["employee"]))
    assert (ex.severity, ex.date) == ("info", s["date"])
    line = result.line_for(s["employee"])
    assert line.regular_pay == D(s["salary"])
    assert f"x {s['days']} scheduled working days" in only(steps(line, "Salary (prorated)")).formula


# 16 ------------------------------------------------------------------------------------------

def test_16_exit_prorates_and_settles_loan_up_to_cap(result, scenario):
    s = scenario(16)
    ex = only(result.exceptions_for("EXIT_FINAL_SETTLEMENT", s["employee"]))
    assert (ex.severity, ex.date) == ("needs_review", s["date"])
    assert f"${s['unsettled']} still owed" in ex.message
    line = result.line_for(s["employee"])
    assert line.regular_pay == D(s["salary"])
    assert line.loan_deductions == D(s["loan_deducted"])
    assert only(steps(line, "Loan final balance")).formula == "$200.00 (balance after: $600.00)"


# 17 ------------------------------------------------------------------------------------------

@pytest.mark.django_db
def test_17_review_gate(tmp_path):
    from core import services
    from core.models import PayrollExceptionRecord

    period = services.seed_demo(output_dir=tmp_path)
    run = services.run_payroll(period)
    with pytest.raises(services.FinalizeRefused):
        services.finalize(run)
    for x in run.exceptions.filter(severity="needs_review"):
        services.review_exception(x.pk, "approve", "checked")
    assert services.finalize(run).status == "finalized"
    assert not PayrollExceptionRecord.objects.filter(run=run, severity="needs_review", status="open").exists()


# 18 ------------------------------------------------------------------------------------------

def test_18_custom_rules_from_config(result, scenario, inputs, rules):
    s = scenario(18)
    applied = result.exceptions_for("CUSTOM_RULE_APPLIED")
    assert [e.severity for e in applied] == ["info", "info"]
    line = result.line_for(s["employee"])
    assert only(steps(line, "Retirement contribution")).amount == D(s["retirement"])
    assert only(steps(line, "Company deduction table")).amount == D(s["table"])
    assert line.custom_deductions == D(s["retirement"]) + D(s["table"])

    # The cap and the brackets come from config too.
    changed = rules.with_changes(**{"custom_rules": [
        {"type": "percent_of_gross", "label": "Pension", "rate_percent": "10", "cap": "100.00"},
        {"type": "table_lookup", "label": "Club fee", "brackets": [{"from": "0", "to": None, "flat": "2.00"}]},
    ]})
    line = run_payroll(inputs, changed).line_for(s["employee"])
    assert only(steps(line, "Pension")).amount == D("100.00")  # 10% of 1380.00 capped
    assert only(steps(line, "Club fee")).amount == D("2.00")
