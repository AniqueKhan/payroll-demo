"""Golden end-to-end test: the full seeded period always produces these numbers."""
from collections import Counter
from decimal import Decimal as D

import pytest

EXPECTED_BY_STORE = {
    "A": {"hours": D("555.80"), "gross": D("9572.09")},
    "B": {"hours": D("408.46"), "gross": D("7197.40")},
    "C": {"hours": D("417.02"), "gross": D("7448.51")},
}
EXPECTED_TOTALS = {
    "regular_pay": D("24240.40"),
    "overtime_pay": D("107.60"),
    "addons": D("190.00"),
    "penalties": D("30.00"),
    "absence_deductions": D("320.00"),
    "loan_deductions": D("626.00"),
    "custom_deductions": D("996.12"),
    "gross": D("24218.00"),
    "net": D("22565.88"),
}
EXPECTED_BY_SEVERITY = {"needs_review": 6, "info": 16}
EXPECTED_BY_CODE = {
    "ABSENCE_INFORMED": 2, "ABSENCE_UNINFORMED": 1, "ADDON_OT_RECALC": 1, "BLENDED_RATE_OVERTIME": 1,
    "CUSTOM_RULE_APPLIED": 2, "DUPLICATE_PUNCH": 1, "EXIT_FINAL_SETTLEMENT": 1, "HALF_DAY": 1,
    "LATE_ARRIVAL": 1, "LOAN_CAPPED": 1, "LOAN_DEDUCTED": 1, "MALFORMED_ROW": 1, "MISSED_CLOCK_OUT": 1,
    "MULTI_LOCATION_OVERTIME": 2, "NEW_HIRE_PRORATED": 1, "ODD_PUNCH_COUNT": 1, "OVERNIGHT_SHIFT": 1,
    "UNKNOWN_COLUMN": 1, "WRONG_LOCATION": 1,
}


def test_golden_totals_per_store(result):
    assert result.totals["by_location"] == EXPECTED_BY_STORE
    assert {k: result.totals[k] for k in EXPECTED_TOTALS} == EXPECTED_TOTALS
    assert result.totals["employees"] == 18


def test_golden_exception_counts(result):
    assert dict(Counter(e.severity for e in result.exceptions)) == EXPECTED_BY_SEVERITY
    assert dict(Counter(e.code for e in result.exceptions)) == EXPECTED_BY_CODE


def test_store_totals_reconcile_with_lines(result):
    assert sum(s["gross"] for s in result.totals["by_location"].values()) == result.totals["gross"]
    for line in result.lines:
        assert sum((b["gross"] for b in line.by_location.values()), D(0)) == line.gross, line.employee_id
        assert line.net == line.gross - line.penalties - line.loan_deductions - line.custom_deductions
        # Every amount on the line is explained by the trail, ending in the net pay step.
        assert line.trail[-1].label == "Net pay" and line.trail[-1].amount == line.net


def test_money_is_decimal_rounded_to_cents(result):
    for line in result.lines:
        for field in ("regular_pay", "overtime_pay", "addons", "penalties", "absence_deductions",
                      "loan_deductions", "custom_deductions", "gross", "net"):
            value = getattr(line, field)
            assert isinstance(value, D) and value == value.quantize(D("0.01")), (line.employee_id, field)


def test_generator_is_deterministic(tmp_path, demo_dir):
    from core.demo_data.generator import generate

    generate(tmp_path)
    for f in sorted(demo_dir.iterdir()):
        assert (tmp_path / f.name).read_bytes() == f.read_bytes(), f.name


def test_engine_has_no_django_imports():
    import pathlib

    engine = pathlib.Path(__file__).resolve().parent.parent / "engine"
    for path in engine.rglob("*.py"):
        assert "django" not in path.read_text(), path


@pytest.mark.django_db
def test_seeded_db_run_matches_golden(tmp_path):
    from core import services

    run = services.run_payroll(services.seed_demo(output_dir=tmp_path))
    assert run.status == "draft"
    assert {k: {f: D(v) for f, v in s.items()} for k, s in run.totals["by_location"].items()} == EXPECTED_BY_STORE
    assert D(run.totals["net"]) == EXPECTED_TOTALS["net"]
    assert dict(Counter(run.exceptions.values_list("severity", flat=True))) == EXPECTED_BY_SEVERITY
    assert run.lines.count() == 18
