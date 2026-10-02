"""Rules live in config: changing a value in the rules changes the output with no code change."""
from decimal import Decimal as D

import pytest
import yaml

from core.engine.config import DEFAULT_RULES_PATH, RulesError, load_rules
from core.engine.run import run_payroll


def test_late_penalty_comes_from_rules_file(inputs, result, tmp_path):
    raw = yaml.safe_load(DEFAULT_RULES_PATH.read_text())
    raw["late"]["penalty_per_late"] = "7.50"
    edited = tmp_path / "rules.yaml"
    edited.write_text(yaml.safe_dump(raw))

    changed = run_payroll(inputs, load_rules(edited))
    before, after = result.line_for("E02"), changed.line_for("E02")
    assert (before.penalties, after.penalties) == (D("5.00"), D("7.50"))
    assert after.net == before.net - D("2.50")


def test_grace_period_comes_from_rules(inputs, rules):
    # E05 clocked in 6 minutes late: inside a 10 minute grace, outside a 5 minute one.
    res = run_payroll(inputs, rules.with_changes(**{"late.grace_minutes": 5}))
    assert res.exceptions_for("LATE_ARRIVAL", "E05")
    assert res.line_for("E05").penalties == D("5.00")


def test_overtime_threshold_and_multiplier_come_from_rules(inputs, rules):
    res = run_payroll(inputs, rules.with_changes(**{"overtime.weekly_threshold_hours": "44",
                                                    "overtime.multiplier": "2.0"}))
    assert res.line_for("E06").overtime_pay == D("0.00")  # 44 h no longer over the threshold
    assert res.line_for("E15").overtime_pay == D("17.00")  # 1.0 x $17.00 x 1 h


def test_loan_cap_comes_from_rules(inputs, rules):
    res = run_payroll(inputs, rules.with_changes(**{"loans.max_deduction_percent_of_net": "50"}))
    assert res.line_for("E16").loan_deductions == D("400.00")
    assert not res.exceptions_for("LOAN_CAPPED")


def test_invalid_rules_are_rejected(rules):
    with pytest.raises(RulesError):
        rules.with_changes(**{"overtime.method": "highest_rate"})
    with pytest.raises(ValueError):
        rules.with_changes(custom_rules=[{"type": "flat_tax", "label": "x"}])
