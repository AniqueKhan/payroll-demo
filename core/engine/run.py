"""Orchestrator: ``run_payroll(inputs, rules) -> PayrollResult``."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from typing import Optional

from . import exceptions as exc
from .config import Rules
from .custom_rules import apply_custom_rules
from .explain import fmt_money
from .finance_stage import run_finance_stage
from .hr_stage import run_hr_stage
from .importers import get_importer
from .importers.base import FileLike
from .types import (
    ZERO, Adjustments, Employee, LeaveRecord, LoanAdvance, Location, PayAddOn, PayrollException, PayrollResult,
    Period, Punch, ScheduledShift,
)

SEVERITY_ORDER = {exc.BLOCKING: 0, exc.NEEDS_REVIEW: 1, exc.INFO: 2}


@dataclass
class PayrollInputs:
    period: Period
    locations: list[Location]
    employees: list[Employee]
    schedules: list[ScheduledShift]
    time_files: dict[str, FileLike]  # location code -> export; a missing key means no file arrived
    leaves: list[LeaveRecord] = field(default_factory=list)
    addons: list[PayAddOn] = field(default_factory=list)
    loans: list[LoanAdvance] = field(default_factory=list)


def make_period(start, rules: Rules) -> Period:
    if start.weekday() != rules.week_start:
        raise ValueError(f"Period start {start} is not on the configured week start day")
    return Period(start, start + timedelta(days=rules.period_length_days - 1))


def import_time_files(inputs: PayrollInputs, rules: Rules) -> tuple[list[Punch], list[PayrollException], set[str]]:
    punches: list[Punch] = []
    found: list[PayrollException] = []
    missing: set[str] = set()
    expected = set(rules.expected_locations) or {loc.code for loc in inputs.locations}
    for loc in inputs.locations:
        source = inputs.time_files.get(loc.code)
        if source is None:
            if loc.code in expected:
                missing.add(loc.code)
                found.append(exc.make(
                    "STORE_FILE_MISSING",
                    f"Store {loc.code} ({loc.name}) has no time clock import for {inputs.period.start} to "
                    f"{inputs.period.end}. Nothing is estimated; import the file and re-run",
                    location_code=loc.code,
                ))
            continue
        importer = get_importer(loc.export_format)(loc.code, inputs.employees)
        loc_punches, loc_found = importer.parse(source)
        if any(e.code == "FILE_UNREADABLE" for e in loc_found):
            missing.add(loc.code)
        punches.extend(loc_punches)
        found.extend(loc_found)
    return punches, found, missing


def run_payroll(inputs: PayrollInputs, rules: Rules, adjustments: Optional[Adjustments] = None) -> PayrollResult:
    """Run the full pipeline. ``adjustments`` carry reviewer decisions that replace the engine's defaults."""
    period = inputs.period
    adjustments = adjustments or Adjustments()
    applied: dict[str, str] = {}  # exception key -> resolution text, filled by the stages
    punches, found, missing = import_time_files(inputs, rules)
    # Bad rows that belong to a known employee may change someone's pay: the HR stage settles them.
    bad_rows = [e for e in found if e.code == "MALFORMED_ROW" and e.employee_id]
    found = [e for e in found if not (e.code == "MALFORMED_ROW" and e.employee_id)]

    hr, hr_found = run_hr_stage(inputs.employees, inputs.schedules, punches, inputs.leaves, period, rules, missing,
                                bad_rows, adjustments, applied)
    found.extend(hr_found)
    found.extend(run_finance_stage(hr, inputs.addons, inputs.loans, period, rules, adjustments, applied))

    lines = [hr[k].line for k in sorted(hr)]
    found.extend(apply_custom_rules(rules.custom_rules, lines))

    for line in lines:
        line.stage = "custom"
        line.net = line.gross - line.penalties - line.loan_deductions - line.custom_deductions
        line.step("Net pay", f"gross {fmt_money(line.gross)} - penalties {fmt_money(line.penalties)} - loans "
                  f"{fmt_money(line.loan_deductions)} - custom {fmt_money(line.custom_deductions)} = "
                  f"{fmt_money(line.net)}", line.net)
        if line.net < 0:
            found.append(exc.make("NEGATIVE_NET", f"Net pay {fmt_money(line.net)} is negative",
                                  employee_id=line.employee_id))

    # The original exception stays visible, marked as settled by the reviewer's adjustment.
    for e in found:
        if e.key in applied:
            e.adjusted = True
            e.resolution = applied[e.key]

    found.sort(key=lambda e: (SEVERITY_ORDER[e.severity], e.code, e.employee_id or "", str(e.date or "")))
    return PayrollResult(lines=lines, exceptions=found, period=period, totals=compute_totals(lines, found, inputs))


def compute_totals(lines, found, inputs: PayrollInputs) -> dict:
    by_location: dict[str, dict[str, Decimal]] = {
        loc.code: {"hours": ZERO, "gross": ZERO} for loc in inputs.locations
    }
    for line in lines:
        for code, bucket in line.by_location.items():
            slot = by_location.setdefault(code, {"hours": ZERO, "gross": ZERO})
            slot["hours"] += bucket["hours"]
            slot["gross"] += bucket["gross"]
    fields = ("regular_pay", "overtime_pay", "addons", "penalties", "absence_deductions", "loan_deductions",
              "custom_deductions", "gross", "net")
    totals = {f: sum((getattr(line, f) for line in lines), ZERO) for f in fields}
    totals["employees"] = len(lines)
    totals["by_location"] = by_location
    totals["exceptions_by_severity"] = dict(Counter(e.severity for e in found))
    return totals
