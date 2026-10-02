"""Bridge between the ORM and the pure-Python engine, plus the review gate."""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Optional

from django.db import transaction
from django.utils import timezone

from . import models as m
from .demo_data import generator
from .engine import inputs as engine_inputs
from .engine import types as t
from .engine.config import DEFAULT_RULES_PATH, Rules, load_rules
from .engine.exceptions import BLOCKING, NEEDS_REVIEW
from .engine.run import PayrollInputs, make_period
from .engine.run import run_payroll as run_engine


class PayrollError(Exception):
    pass


class FinalizeRefused(PayrollError):
    def __init__(self, reasons: list[str]):
        self.reasons = reasons
        super().__init__("; ".join(reasons))


# ---------------------------------------------------------------- seeding

@transaction.atomic
def seed_demo(missing_store: Optional[str] = None, output_dir: Optional[Path] = None) -> m.PayPeriod:
    """Generate the demo files and load master data, schedules and inputs into the DB (replacing old data)."""
    output_dir = Path(output_dir or generator.DEFAULT_OUTPUT)
    manifest = generator.generate(output_dir, missing_store)

    for model in (m.PayrollRun, m.ImportBatch, m.ScheduledShift, m.LeaveRecord, m.PayAddOn, m.LoanAdvance,
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

    for code, name in manifest["time_files"].items():
        path = output_dir / name
        if not path.exists():
            continue  # missing store: no import batch, the engine raises a blocking exception
        rows = max(sum(1 for _ in open(path, encoding="utf-8")) - 1, 0)
        m.ImportBatch.objects.create(location=locations[code], period=period, filename=str(path),
                                     status="imported", row_count=rows)
    return period


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


@transaction.atomic
def run_payroll(period: m.PayPeriod, rules_path: Optional[Path] = None) -> m.PayrollRun:
    """Run the engine for ``period`` and store the result as a draft run, replacing any earlier draft."""
    if period.runs.filter(status=m.PayrollRun.FINALIZED).exists():
        raise PayrollError(f"Payroll for {period} is already finalized")
    rules = load_rules(rules_path or DEFAULT_RULES_PATH)
    result = run_engine(build_inputs(period, rules), rules)

    period.runs.filter(status=m.PayrollRun.DRAFT).delete()
    run = m.PayrollRun.objects.create(period=period, rules_snapshot=rules.raw, totals=_json(result.totals))
    employees = {e.employee_id: e for e in m.Employee.objects.all()}
    locations = {x.code: x for x in m.Location.objects.all()}
    m.PayrollLineRecord.objects.bulk_create(
        m.PayrollLineRecord(
            run=run, employee=employees[line.employee_id],
            regular_pay=line.regular_pay, overtime_pay=line.overtime_pay, addons=line.addons,
            penalties=line.penalties, absence_deductions=line.absence_deductions,
            loan_deductions=line.loan_deductions, custom_deductions=line.custom_deductions,
            gross=line.gross, net=line.net, by_location=_json(line.by_location),
            trail=[s.to_dict() for s in line.trail],
        )
        for line in result.lines)
    m.PayrollExceptionRecord.objects.bulk_create(
        m.PayrollExceptionRecord(
            run=run, code=x.code, severity=x.severity,
            employee=employees.get(x.employee_id) if x.employee_id else None,
            location=locations.get(x.location_code) if x.location_code else None,
            date=x.date, message=x.message, auto_resolved=x.auto_resolved, resolution=x.resolution,
        )
        for x in result.exceptions)
    return run


# ---------------------------------------------------------------- review gate

def review_exception(exception_id: int, action: str, note: str = "") -> m.PayrollExceptionRecord:
    """Approve (accept the engine's handling) or override (a human decides otherwise, note required)."""
    record = m.PayrollExceptionRecord.objects.select_related("run").get(pk=exception_id)
    if record.run.status == m.PayrollRun.FINALIZED:
        raise PayrollError("Run is finalized; exceptions can no longer be reviewed")
    statuses = {"approve": m.PayrollExceptionRecord.APPROVED, "override": m.PayrollExceptionRecord.OVERRIDDEN}
    if action not in statuses:
        raise PayrollError(f"Unknown action {action!r}; use 'approve' or 'override'")
    if action == "override" and not note.strip():
        raise PayrollError("An override needs a note explaining the decision")
    record.status = statuses[action]
    record.review_note = note
    record.reviewed_at = timezone.now()
    record.save(update_fields=["status", "review_note", "reviewed_at"])
    return record


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


@transaction.atomic
def finalize(run: m.PayrollRun) -> m.PayrollRun:
    run = m.PayrollRun.objects.select_for_update().get(pk=run.pk)
    if run.status == m.PayrollRun.FINALIZED:
        raise PayrollError("Run is already finalized")
    reasons = finalize_blockers(run)
    if reasons:
        raise FinalizeRefused(reasons)
    # Post loan/advance deductions so the remainder carries into the next period.
    for line in run.lines.all():
        for step in line.trail:
            loan_id = (step.get("refs") or {}).get("loan")
            if loan_id and step.get("amount"):
                loan = m.LoanAdvance.objects.select_for_update().get(pk=int(loan_id))
                loan.paid_to_date += Decimal(step["amount"])
                loan.save(update_fields=["paid_to_date"])
    run.status = m.PayrollRun.FINALIZED
    run.finalized_at = timezone.now()
    run.save(update_fields=["status", "finalized_at"])
    return run
