"""Bridge between the ORM and the pure-Python engine, plus the review gate."""
from __future__ import annotations

import csv
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Optional

from django.db import transaction
from django.utils import timezone

from . import models as m
from .demo_data import generator
from .engine import inputs as engine_inputs
from .engine import types as t
from .engine.config import DEFAULT_RULES_PATH, Rules, load_rules
from .engine.exceptions import BLOCKING, INFO, NEEDS_REVIEW
from .engine.explain import fmt_money
from .engine.importers import get_importer
from .engine.run import PayrollInputs, make_period
from .engine.run import run_payroll as run_engine


class PayrollError(Exception):
    pass


class FinalizeRefused(PayrollError):
    def __init__(self, reasons: list[str]):
        self.reasons = reasons
        super().__init__("; ".join(reasons))


class DecisionError(PayrollError):
    pass


# What a reviewer may do with each needs_review code. ``value`` names the field "approve with value" takes;
# None means only "approve as is". Codes not listed here (needs_review) can only be approved as is.
DECISION_VALUES: dict[str, Optional[str]] = {
    "MISSED_CLOCK_OUT": "hours",
    "ODD_PUNCH_COUNT": "hours",
    "MALFORMED_ROW": "hours",
    "WRONG_LOCATION": "location",
    "ABSENCE_UNINFORMED": "leave_type",
    "LOAN_CAPPED": "amount",
    "EXIT_FINAL_SETTLEMENT": None,
}


def value_kind(record: "m.PayrollExceptionRecord") -> Optional[str]:
    """The kind of value this exception can be approved with, or None for approve-as-is only."""
    kind = DECISION_VALUES.get(record.code)
    if kind == "hours" and record.date is None:
        return None  # no day to set hours on
    return kind


# ---------------------------------------------------------------- seeding

@transaction.atomic
def seed_demo(missing_store: Optional[str] = None, output_dir: Optional[Path] = None) -> m.PayPeriod:
    """Generate the demo files and load master data, schedules and inputs into the DB (replacing old data)."""
    output_dir = Path(output_dir or generator.DEFAULT_OUTPUT)
    manifest = generator.generate(output_dir, missing_store)

    # Sandboxes hold runs and decisions computed from the old inputs: they would no longer match.
    for model in (m.DemoSandbox, m.PayrollRun, m.ImportBatch, m.ScheduledShift, m.LeaveRecord, m.PayAddOn, m.LoanAdvance,
                  m.Employee, m.PayPeriod, m.Location):
        model.objects.all().delete()

    rules = load_rules(DEFAULT_RULES_PATH)
    start = date.fromisoformat(manifest["period_start"])
    p = make_period(start, rules)
    period = m.PayPeriod.objects.create(start_date=p.start, end_date=p.end)

    locations = {}
    for loc in engine_inputs.read_locations(output_dir / "locations.json"):
        locations[loc.code] = m.Location.objects.create(
            code=loc.code, name=loc.name, timezone=loc.timezone, export_format=loc.export_format)

    employees = {}
    for e in engine_inputs.read_employees(output_dir / "employees.json"):
        employees[e.id] = m.Employee.objects.create(
            employee_id=e.id, name=e.name, home_location=locations[e.home_location], pay_type=e.pay_type,
            rates={k: str(v) for k, v in e.rates.items()}, salary_per_period=e.salary_per_period,
            hire_date=e.hire_date, exit_date=e.exit_date, external_ids=e.external_ids)

    m.ScheduledShift.objects.bulk_create(
        m.ScheduledShift(employee=employees[s.employee_id], location=locations[s.location_code], date=s.date,
                         start=s.start.time(), end=s.end.time())
        for s in engine_inputs.read_schedules(output_dir / "schedules.csv"))
    m.LeaveRecord.objects.bulk_create(
        m.LeaveRecord(employee=employees[x.employee_id], date=x.date, leave_type=x.leave_type, paid=x.paid,
                      informed=x.informed)
        for x in engine_inputs.read_leaves(output_dir / "leaves.csv"))
    m.PayAddOn.objects.bulk_create(
        m.PayAddOn(employee=employees[x.employee_id], date=x.date, kind=x.kind, amount=x.amount,
                   include_in_regular_rate=x.include_in_regular_rate)
        for x in engine_inputs.read_addons(output_dir / "addons.csv"))
    m.LoanAdvance.objects.bulk_create(
        m.LoanAdvance(employee=employees[x.employee_id], kind=x.kind, total=x.total, installment=x.installment,
                      start_period=x.start_period, paid_to_date=x.paid_to_date)
        for x in engine_inputs.read_loans(output_dir / "loans_advances.csv"))

    engine_employees = engine_inputs.read_employees(output_dir / "employees.json")
    for code, name in manifest["time_files"].items():
        path = output_dir / name
        if not path.exists():
            continue  # missing store: no import batch, the engine raises a blocking exception
        store_raw_rows(locations[code], period, path, engine_employees)
    return period


def store_raw_rows(location: m.Location, period: m.PayPeriod, path: Path, employees) -> m.ImportBatch:
    """Keep the export exactly as received, row by row, with each row's parse problem (if any)."""
    importer = get_importer(location.export_format)(location.code, employees)
    _, found = importer.parse(path)
    errors: dict[int, list[str]] = {}
    for x in found:
        for _, row in x.rows:
            errors.setdefault(row, []).append(x.message)
    with open(path, encoding="utf-8-sig", newline="") as fh:
        rows = [r for r in csv.reader(fh) if r]  # blank lines are skipped, like the importers do
    header, body = (rows[0], rows[1:]) if rows else ([], [])
    header = [h.strip() for h in header]
    batch = m.ImportBatch.objects.create(
        location=location, period=period, filename=str(path), status="imported", row_count=len(body),
        columns=header, unknown_columns=[c for c in header if c not in importer.required_columns])
    m.RawImportRow.objects.bulk_create(
        m.RawImportRow(batch=batch, row_number=n, raw=dict(zip(header, values)),
                       parse_error="; ".join(errors.get(n, [])))
        for n, values in enumerate(body, start=2))
    return batch


# ---------------------------------------------------------------- ORM -> engine

def build_inputs(period: m.PayPeriod, rules: Rules) -> PayrollInputs:
    p = make_period(period.start_date, rules)
    locations = [t.Location(x.code, x.name, x.timezone, x.export_format) for x in m.Location.objects.all()]
    employees = [
        engine_inputs.employee_from_dict({
            "id": e.employee_id, "name": e.name, "home_location": e.home_location.code, "pay_type": e.pay_type,
            "rates": e.rates, "salary_per_period": e.salary_per_period,
            "hire_date": e.hire_date.isoformat(), "exit_date": e.exit_date.isoformat() if e.exit_date else None,
            "external_ids": e.external_ids,
        })
        for e in m.Employee.objects.select_related("home_location")
    ]
    schedules = [
        engine_inputs.schedule(s.employee.employee_id, s.location.code, s.date, s.start, s.end)
        for s in m.ScheduledShift.objects.filter(date__range=(p.start, p.end)).select_related("employee", "location")
    ]
    time_files = {
        b.location.code: Path(b.filename)
        for b in period.imports.filter(status="imported").select_related("location")
        if Path(b.filename).exists()
    }
    leaves = [t.LeaveRecord(x.employee.employee_id, x.date, x.leave_type, x.paid, x.informed)
              for x in m.LeaveRecord.objects.filter(date__range=(p.start, p.end)).select_related("employee")]
    addons = [t.PayAddOn(x.employee.employee_id, x.date, x.kind, x.amount, x.include_in_regular_rate)
              for x in m.PayAddOn.objects.filter(date__range=(p.start, p.end)).select_related("employee")]
    loans = [t.LoanAdvance(x.employee.employee_id, x.kind, x.total, x.installment, x.start_period,
                           x.paid_to_date, ref=str(x.pk))
             for x in m.LoanAdvance.objects.select_related("employee")]
    return PayrollInputs(p, locations, employees, schedules, time_files, leaves, addons, loans)


def _json(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {k: _json(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json(v) for v in value]
    return value


def decisions(period: m.PayPeriod, sandbox: Optional[m.DemoSandbox] = None):
    """The review decisions of one sandbox (None = baseline) for the period."""
    return period.decisions.filter(sandbox=sandbox)


def decisions_to_adjustments(period: m.PayPeriod, sandbox: Optional[m.DemoSandbox] = None) -> t.Adjustments:
    adj = t.Adjustments()
    for d in decisions(period, sandbox).filter(action=m.ReviewDecision.APPROVE_WITH_VALUE):
        code, emp, on, loc = t.parse_exception_key(d.exception_key)
        kind, value = DECISION_VALUES.get(code), d.value or {}
        if kind == "hours" and on and loc:
            adj.hours_overrides.append(t.HoursOverride(emp, on, loc, Decimal(value["hours"]), d.note))
        elif kind == "location" and on:
            adj.location_overrides.append(t.LocationOverride(emp, on, value["location"], d.note))
        elif kind == "leave_type" and on:
            adj.leave_overrides.append(t.LeaveOverride(emp, on, value["leave_type"], d.note))
        elif kind == "amount":
            adj.loan_overrides.append(t.LoanOverride(emp, value["loan_kind"], Decimal(value["amount"]), d.note))
    return adj


def runs(period: m.PayPeriod, sandbox: Optional[m.DemoSandbox] = None):
    """The runs of one sandbox (None = baseline) for the period."""
    return period.runs.filter(sandbox=sandbox)


@transaction.atomic
def run_payroll(period: m.PayPeriod, rules_path: Optional[Path] = None,
                sandbox: Optional[m.DemoSandbox] = None) -> m.PayrollRun:
    """Run the engine for ``period`` with this sandbox's review decisions applied (None = baseline), and store
    the result as that sandbox's draft run, replacing its earlier draft. Exceptions with a decision are marked
    approved. Other sandboxes and the baseline are never touched."""
    if runs(period, sandbox).filter(status=m.PayrollRun.FINALIZED).exists():
        raise PayrollError(f"Payroll for {period} is finalized; reopen it before re-running")
    rules = load_rules(rules_path or DEFAULT_RULES_PATH)
    result = run_engine(build_inputs(period, rules), rules, decisions_to_adjustments(period, sandbox))

    runs(period, sandbox).filter(status=m.PayrollRun.DRAFT).delete()
    run = m.PayrollRun.objects.create(period=period, sandbox=sandbox, rules_snapshot=rules.raw,
                                      totals=_json(result.totals))
    employees = {e.employee_id: e for e in m.Employee.objects.all()}
    locations = {x.code: x for x in m.Location.objects.all()}
    m.PayrollLineRecord.objects.bulk_create(
        m.PayrollLineRecord(
            run=run, employee=employees[line.employee_id],
            regular_pay=line.regular_pay, overtime_pay=line.overtime_pay, addons=line.addons,
            penalties=line.penalties, absence_deductions=line.absence_deductions,
            loan_deductions=line.loan_deductions, custom_deductions=line.custom_deductions,
            gross=line.gross, net=line.net, hours=line.hours, overtime_hours=line.ot_hours,
            by_location=_json(line.by_location), trail=[s.to_dict() for s in line.trail],
        )
        for line in result.lines)
    m.PayrollExceptionRecord.objects.bulk_create(
        m.PayrollExceptionRecord(
            run=run, code=x.code, severity=x.severity,
            employee=employees.get(x.employee_id) if x.employee_id else None,
            location=locations.get(x.location_code) if x.location_code else None,
            date=x.date, message=x.message, auto_resolved=x.auto_resolved, resolution=x.resolution,
            exception_key=x.key, adjusted=x.adjusted, rows=[list(r) for r in x.rows], context=x.context,
        )
        for x in result.exceptions)
    for d in decisions(period, sandbox):
        run.exceptions.filter(exception_key=d.exception_key).exclude(severity=BLOCKING).update(
            status=m.PayrollExceptionRecord.APPROVED, review_note=d.note, reviewed_at=d.decided_at)
    return run


# ---------------------------------------------------------------- sandboxes and current run

def current_run(period: m.PayPeriod, sandbox: Optional[m.DemoSandbox] = None) -> Optional[m.PayrollRun]:
    return runs(period, sandbox).order_by("-created_at").first()


def demo_period() -> Optional[m.PayPeriod]:
    """The period the demo shows: the one with the latest baseline run."""
    run = m.PayrollRun.objects.filter(sandbox=None).select_related("period").order_by("-created_at").first()
    return run.period if run else None


@transaction.atomic
def create_sandbox(period: m.PayPeriod) -> m.DemoSandbox:
    """A new private copy. Its run is computed by the engine with no decisions, so it equals the baseline."""
    sandbox = m.DemoSandbox.objects.create()
    run_payroll(period, sandbox=sandbox)
    return sandbox


def ensure_sandbox_run(period: m.PayPeriod, sandbox: m.DemoSandbox) -> m.PayrollRun:
    return current_run(period, sandbox) or run_payroll(period, sandbox=sandbox)


def cleanup_sandboxes(older_than: timedelta) -> int:
    """Delete sandboxes not seen for ``older_than``; their runs and decisions go with them. Returns the count."""
    old = m.DemoSandbox.objects.filter(last_seen_at__lt=timezone.now() - older_than)
    count = old.count()
    old.delete()
    return count


def _clean_value(record: m.PayrollExceptionRecord, kind: str, value: dict) -> dict:
    value = value or {}
    if kind == "hours":
        try:
            hours = Decimal(str(value.get("hours", ""))).quantize(Decimal("0.01"))
        except InvalidOperation:
            raise DecisionError("Enter the hours worked as a number")
        if not Decimal(0) <= hours <= Decimal(24):
            raise DecisionError("Hours must be between 0 and 24")
        return {"hours": str(hours)}
    if kind == "location":
        loc = str(value.get("location", "")).upper()
        if loc not in (record.employee.rates or {}):
            raise DecisionError(f"{record.employee.name} has no pay rate at store '{loc}'")
        return {"location": loc}
    if kind == "leave_type":
        leave_type = str(value.get("leave_type", ""))
        if leave_type not in load_rules(DEFAULT_RULES_PATH).leave_types:
            raise DecisionError(f"'{leave_type}' is not a leave type in the rules")
        return {"leave_type": leave_type}
    if kind == "amount":
        try:
            amount = Decimal(str(value.get("amount", ""))).quantize(Decimal("0.01"))
        except InvalidOperation:
            raise DecisionError("Enter the deduction as an amount")
        installment = Decimal(record.context["installment"])
        if not Decimal(0) <= amount <= installment:
            raise DecisionError(f"The deduction must be between $0.00 and the installment {fmt_money(installment)}")
        return {"amount": str(amount), "loan_kind": record.context["loan_kind"]}
    raise DecisionError(f"Unknown value kind {kind!r}")


@transaction.atomic
def decide(period: m.PayPeriod, exception_key: str, action: str, value: Optional[dict] = None,
           note: str = "", sandbox: Optional[m.DemoSandbox] = None) -> m.PayrollRun:
    """Record (or replace) a reviewer decision in one sandbox (None = baseline), then re-run its payroll."""
    run = current_run(period, sandbox)
    if run is None or run.status == m.PayrollRun.FINALIZED:
        raise DecisionError("Payroll for this period is finalized; reopen it to change decisions")
    record = run.exceptions.filter(exception_key=exception_key).select_related("employee").first()
    if record is None:
        raise DecisionError("That exception is not in the current run")
    if record.severity == BLOCKING:
        raise DecisionError("Blocking exceptions cannot be approved: fix the input and re-run")
    if record.severity == INFO:
        raise DecisionError("Handled by a rule: no decision needed")
    if action == m.ReviewDecision.APPROVE:
        value = None
    elif action == m.ReviewDecision.APPROVE_WITH_VALUE:
        kind = value_kind(record)
        if kind is None:
            raise DecisionError(f"{record.code} can only be approved as is")
        value = _clean_value(record, kind, value)
    else:
        raise DecisionError(f"Unknown action {action!r}")
    m.ReviewDecision.objects.update_or_create(
        period=period, sandbox=sandbox, exception_key=exception_key,
        defaults={"action": action, "value": value, "note": note.strip()})
    return run_payroll(period, sandbox=sandbox)


@transaction.atomic
def undo_decision(period: m.PayPeriod, exception_key: str, sandbox: Optional[m.DemoSandbox] = None) -> m.PayrollRun:
    _require_draft(period, sandbox)
    decisions(period, sandbox).filter(exception_key=exception_key).delete()
    return run_payroll(period, sandbox=sandbox)


@transaction.atomic
def clear_decisions(period: m.PayPeriod, sandbox: Optional[m.DemoSandbox] = None) -> m.PayrollRun:
    _require_draft(period, sandbox)
    decisions(period, sandbox).delete()
    return run_payroll(period, sandbox=sandbox)


def _require_draft(period: m.PayPeriod, sandbox: Optional[m.DemoSandbox]) -> None:
    run = current_run(period, sandbox)
    if run is not None and run.status == m.PayrollRun.FINALIZED:
        raise DecisionError("Payroll for this period is finalized; reopen it to change decisions")


def finalize_blockers(run: m.PayrollRun) -> list[str]:
    reasons = []
    blocking = run.exceptions.filter(severity=BLOCKING)
    if blocking.exists():
        codes = ", ".join(sorted(set(blocking.values_list("code", flat=True))))
        reasons.append(f"{blocking.count()} blocking exception(s): {codes}")
    open_review = run.exceptions.filter(severity=NEEDS_REVIEW, status=m.PayrollExceptionRecord.OPEN)
    if open_review.exists():
        reasons.append(f"{open_review.count()} needs_review exception(s) still open")
    return reasons


def _post_loans(run: m.PayrollRun, sign: int) -> None:
    """Add (or, on reopen, take back) this run's loan deductions so capped remainders carry over."""
    for line in run.lines.all():
        for step in line.trail:
            loan_id = (step.get("refs") or {}).get("loan")
            if loan_id and step.get("amount"):
                loan = m.LoanAdvance.objects.select_for_update().get(pk=int(loan_id))
                loan.paid_to_date += sign * Decimal(step["amount"])
                loan.save(update_fields=["paid_to_date"])


def _current_run_locked(period: m.PayPeriod, sandbox: Optional[m.DemoSandbox]) -> m.PayrollRun:
    run = current_run(period, sandbox)
    if run is None:
        raise PayrollError("There is no payroll run to act on")
    return m.PayrollRun.objects.select_for_update().get(pk=run.pk)


@transaction.atomic
def finalize(period: m.PayPeriod, sandbox: Optional[m.DemoSandbox] = None) -> m.PayrollRun:
    """Finalize the current run of one sandbox (None = baseline).

    Only the baseline posts loan deductions to the loan balances. Loan balances are shared inputs, so a
    visitor's sandbox must never change them.
    """
    run = _current_run_locked(period, sandbox)
    if run.status == m.PayrollRun.FINALIZED:
        raise PayrollError("Run is already finalized")
    reasons = finalize_blockers(run)
    if reasons:
        raise FinalizeRefused(reasons)
    if sandbox is None:
        _post_loans(run, +1)
    run.status = m.PayrollRun.FINALIZED
    run.finalized_at = timezone.now()
    run.save(update_fields=["status", "finalized_at"])
    return run


@transaction.atomic
def reopen(period: m.PayPeriod, sandbox: Optional[m.DemoSandbox] = None) -> m.PayrollRun:
    """Demo convenience: put a finalized run back to draft (the baseline also takes back its loan postings)."""
    run = _current_run_locked(period, sandbox)
    if run.status != m.PayrollRun.FINALIZED:
        raise PayrollError("Run is not finalized")
    if sandbox is None:
        _post_loans(run, -1)
    run.status = m.PayrollRun.DRAFT
    run.finalized_at = None
    run.save(update_fields=["status", "finalized_at"])
    return run
