import uuid
from datetime import date
from pathlib import Path

from django.db import models
from django.db.models import Q
from django.utils import timezone

from . import labels


class Location(models.Model):
    code = models.CharField(max_length=10, unique=True)
    name = models.CharField(max_length=100)
    timezone = models.CharField(max_length=64)
    export_format = models.CharField(max_length=32)

    class Meta:
        ordering = ["code"]

    def __str__(self):
        return f"Store {self.code} · {self.name}"


class Employee(models.Model):
    PAY_TYPES = [("hourly", "Hourly"), ("salaried", "Salaried")]

    employee_id = models.CharField(max_length=20, unique=True)
    name = models.CharField(max_length=100)
    home_location = models.ForeignKey(Location, on_delete=models.PROTECT, related_name="employees")
    pay_type = models.CharField(max_length=10, choices=PAY_TYPES)
    rates = models.JSONField(default=dict, blank=True)  # {location_code: "16.00"}
    salary_per_period = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    hire_date = models.DateField()
    exit_date = models.DateField(null=True, blank=True)
    external_ids = models.JSONField(default=dict, blank=True)  # {location_code: store's own identifier}

    class Meta:
        ordering = ["employee_id"]

    def __str__(self):
        return f"{self.employee_id} {self.name}"


class PayPeriod(models.Model):
    start_date = models.DateField(unique=True)
    end_date = models.DateField()

    class Meta:
        ordering = ["-start_date"]

    def __str__(self):
        return self.label

    @property
    def label(self) -> str:
        """'Sep 7 to 20, 2026'."""
        return labels.period_label(self.start_date, self.end_date, with_year=True)

    @property
    def short_label(self) -> str:
        """'Sep 7 to 20'."""
        return labels.period_label(self.start_date, self.end_date)


class ScheduledShift(models.Model):
    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name="schedules")
    location = models.ForeignKey(Location, on_delete=models.PROTECT)
    date = models.DateField()
    start = models.TimeField()
    end = models.TimeField()  # earlier than start means the shift ends the next day

    class Meta:
        ordering = ["date", "start"]

    def __str__(self):
        return (f"{self.employee} · Store {self.location.code} · {labels.day_date(self.date)} · "
                f"{self.start:%H:%M} to {self.end:%H:%M}")


class LeaveRecord(models.Model):
    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name="leaves")
    date = models.DateField()
    leave_type = models.CharField(max_length=32)
    paid = models.BooleanField()
    informed = models.BooleanField(default=True)

    def __str__(self):
        notice = "" if self.informed else ", not informed"
        return (f"{self.employee} · {labels.short_date(self.date)} · {self.leave_type} leave "
                f"({'paid' if self.paid else 'unpaid'}{notice})")


class PayAddOn(models.Model):
    KINDS = [("commission", "Commission"), ("bonus", "Bonus")]

    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name="addons")
    date = models.DateField()
    kind = models.CharField(max_length=20, choices=KINDS)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    include_in_regular_rate = models.BooleanField(default=False)

    def __str__(self):
        return f"{self.employee} · {self.kind} {labels.money(self.amount)} · {labels.short_date(self.date)}"


class LoanAdvance(models.Model):
    KINDS = [("loan", "Loan"), ("advance", "Advance")]

    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name="loans")
    kind = models.CharField(max_length=10, choices=KINDS)
    total = models.DecimalField(max_digits=10, decimal_places=2)
    installment = models.DecimalField(max_digits=10, decimal_places=2)
    start_period = models.DateField()
    paid_to_date = models.DecimalField(max_digits=10, decimal_places=2, default=0)

    def __str__(self):
        return (f"{self.employee} · {self.kind} {labels.money(self.total)} · "
                f"{labels.money(self.installment)} per period")


class ImportBatch(models.Model):
    STATUSES = [("imported", "Imported"), ("failed", "Failed")]

    location = models.ForeignKey(Location, on_delete=models.PROTECT, related_name="imports")
    period = models.ForeignKey(PayPeriod, on_delete=models.CASCADE, related_name="imports")
    filename = models.CharField(max_length=500)
    status = models.CharField(max_length=10, choices=STATUSES, default="imported")
    row_count = models.PositiveIntegerField(default=0)
    imported_at = models.DateTimeField(auto_now_add=True)
    columns = models.JSONField(default=list)  # header exactly as exported
    unknown_columns = models.JSONField(default=list)

    class Meta:
        ordering = ["location__code"]

    def __str__(self):
        return (f"Store {self.location.code} · {self.basename} · {self.period.short_label} · "
                f"{self.row_count} rows")

    @property
    def basename(self) -> str:
        return Path(self.filename).name


class RawImportRow(models.Model):
    """One row of a store export, kept exactly as it arrived so the UI can show the 'before'."""
    batch = models.ForeignKey(ImportBatch, on_delete=models.CASCADE, related_name="raw_rows")
    row_number = models.PositiveIntegerField()  # line number in the file (header is 1)
    raw = models.JSONField()  # {column: value as exported}
    parse_error = models.TextField(blank=True, default="")
    # False for rows that belong to another pay period (exports spill a day past both edges).
    in_period = models.BooleanField(default=True)

    class Meta:
        ordering = ["batch", "row_number"]
        constraints = [models.UniqueConstraint(fields=["batch", "row_number"], name="unique_raw_row")]

    def __str__(self):
        values = ", ".join(str(v) for v in self.raw.values())
        return f"Store {self.batch.location.code} · row {self.row_number} · {values}"


class DemoSandbox(models.Model):
    """One visitor's private copy of the demo: their payroll run and review decisions.

    Inputs (employees, schedules, raw rows, leaves, add-ons, loans) stay shared and read-only.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["-last_seen_at"]

    def __str__(self):
        seen = timezone.localtime(self.last_seen_at)
        return f"Visitor copy {self.short_id} · last seen {seen:%b} {seen.day}, {seen:%H:%M}"

    @property
    def short_id(self) -> str:
        return str(self.id)[:8]


class PayrollRun(models.Model):
    DRAFT, FINALIZED = "draft", "finalized"
    STATUSES = [(DRAFT, "Draft"), (FINALIZED, "Finalized")]

    period = models.ForeignKey(PayPeriod, on_delete=models.CASCADE, related_name="runs")
    # NULL = the baseline run, which the web never modifies.
    sandbox = models.ForeignKey(DemoSandbox, on_delete=models.CASCADE, null=True, blank=True, related_name="runs")
    status = models.CharField(max_length=10, choices=STATUSES, default=DRAFT)
    created_at = models.DateTimeField(auto_now_add=True)
    finalized_at = models.DateTimeField(null=True, blank=True)
    rules_snapshot = models.JSONField(default=dict)
    totals = models.JSONField(default=dict)
    # File-level notes (e.g. an extra column in a store export): shown on the Inputs screen, never counted.
    file_notes = models.JSONField(default=list)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            # One draft per (period, sandbox). Separate constraints because NULLs are distinct in UNIQUE.
            models.UniqueConstraint(fields=["period"], condition=Q(status="draft", sandbox__isnull=True),
                                    name="one_baseline_draft_per_period"),
            models.UniqueConstraint(fields=["period", "sandbox"], condition=Q(status="draft", sandbox__isnull=False),
                                    name="one_sandbox_draft_per_period"),
        ]

    def __str__(self):
        where = f"Visitor copy {str(self.sandbox_id)[:8]}" if self.sandbox_id else "Baseline"
        return f"{where} · {self.period} · {self.get_status_display().lower()}"


class PayrollLineRecord(models.Model):
    run = models.ForeignKey(PayrollRun, on_delete=models.CASCADE, related_name="lines")
    employee = models.ForeignKey(Employee, on_delete=models.PROTECT)
    regular_pay = models.DecimalField(max_digits=12, decimal_places=2)
    overtime_pay = models.DecimalField(max_digits=12, decimal_places=2)
    addons = models.DecimalField(max_digits=12, decimal_places=2)
    penalties = models.DecimalField(max_digits=12, decimal_places=2)
    absence_deductions = models.DecimalField(max_digits=12, decimal_places=2)
    loan_deductions = models.DecimalField(max_digits=12, decimal_places=2)
    custom_deductions = models.DecimalField(max_digits=12, decimal_places=2)
    gross = models.DecimalField(max_digits=12, decimal_places=2)
    net = models.DecimalField("net before taxes", max_digits=12, decimal_places=2)
    hours = models.DecimalField(max_digits=8, decimal_places=2, default=0)
    overtime_hours = models.DecimalField(max_digits=8, decimal_places=2, default=0)
    by_location = models.JSONField(default=dict)
    by_department = models.JSONField(default=dict)  # {store: {department: {hours, pay fields, gross}}}
    trail = models.JSONField(default=list)

    class Meta:
        ordering = ["employee__employee_id"]

    @property
    def regular_hours(self):
        return self.hours - self.overtime_hours

    @property
    def deductions(self):
        return self.penalties + self.loan_deductions + self.custom_deductions

    def __str__(self):
        return (f"{self.employee} · {self.run.period.short_label} · gross {labels.money(self.gross)} · "
                f"net before taxes {labels.money(self.net)}")


class PayrollExceptionRecord(models.Model):
    SEVERITIES = [("info", "Info"), ("needs_review", "Needs review"), ("blocking", "Blocking"),
                  ("attendance", "Attendance to discuss")]
    OPEN, APPROVED, OVERRIDDEN = "open", "approved", "overridden"
    STATUSES = [(OPEN, "Open"), (APPROVED, "Approved"), (OVERRIDDEN, "Overridden")]

    run = models.ForeignKey(PayrollRun, on_delete=models.CASCADE, related_name="exceptions")
    exception_key = models.CharField(max_length=120, db_index=True)  # code|employee_id|date|location_code
    code = models.CharField(max_length=40)
    severity = models.CharField(max_length=12, choices=SEVERITIES)
    employee = models.ForeignKey(Employee, on_delete=models.PROTECT, null=True, blank=True)
    location = models.ForeignKey(Location, on_delete=models.PROTECT, null=True, blank=True)
    date = models.DateField(null=True, blank=True)
    message = models.TextField()
    auto_resolved = models.BooleanField(default=False)
    resolution = models.TextField(null=True, blank=True)
    status = models.CharField(max_length=12, choices=STATUSES, default=OPEN)
    review_note = models.TextField(blank=True, default="")
    reviewed_at = models.DateTimeField(null=True, blank=True)
    adjusted = models.BooleanField(default=False)  # a reviewer adjustment changed the calculation
    rows = models.JSONField(default=list)  # [[location_code, row_number], ...] of the raw rows involved
    context = models.JSONField(default=dict)  # suggested hours, loan installment, stores, ...

    class Meta:
        ordering = ["id"]

    def __str__(self):
        parts = [labels.code_title(self.code)]
        if self.employee_id:
            parts.append(str(self.employee))
        if self.location_id:
            parts.append(f"Store {self.location.code}")
        if self.date:
            parts.append(labels.short_date(self.date))
        state = labels.SEVERITY_LABELS.get(self.severity, self.severity).lower()
        if self.severity == "needs_review":
            state += f" ({self.get_status_display().lower()})"
        parts.append(state)
        return " · ".join(parts)


class ReviewDecision(models.Model):
    """A reviewer's call on one exception. Belongs to the period, so it survives re-runs."""
    APPROVE, APPROVE_WITH_VALUE = "approve", "approve_with_value"
    ACTIONS = [(APPROVE, "Approve as is"), (APPROVE_WITH_VALUE, "Approve with value")]

    period = models.ForeignKey(PayPeriod, on_delete=models.CASCADE, related_name="decisions")
    # NULL = baseline decision (management commands only; the web always writes to a sandbox).
    sandbox = models.ForeignKey(DemoSandbox, on_delete=models.CASCADE, null=True, blank=True,
                                related_name="decisions")
    exception_key = models.CharField(max_length=120)
    action = models.CharField(max_length=20, choices=ACTIONS)
    value = models.JSONField(null=True, blank=True)  # {"hours": "8.00"} / {"location": "A"} / ...
    note = models.TextField(blank=True, default="")
    decided_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["decided_at"]
        constraints = [
            models.UniqueConstraint(fields=["period", "exception_key"], condition=Q(sandbox__isnull=True),
                                    name="one_baseline_decision_per_exception"),
            models.UniqueConstraint(fields=["period", "sandbox", "exception_key"],
                                    condition=Q(sandbox__isnull=False), name="one_sandbox_decision_per_exception"),
        ]

    def __str__(self):
        code, employee_id, on, loc = (self.exception_key.split("|") + ["", "", "", ""])[:4]
        parts = [labels.code_title(code)] + [p for p in (employee_id, f"Store {loc}" if loc else "") if p]
        if on:
            parts.append(labels.short_date(date.fromisoformat(on)))
        parts.append(self.summary)
        parts.append(f"visitor copy {str(self.sandbox_id)[:8]}" if self.sandbox_id else "baseline")
        return " · ".join(parts)

    @property
    def summary(self) -> str:
        """'approved as is', 'approved with 8.00 h', 'approved with Store A', ..."""
        value = self.value or {}
        if self.action != self.APPROVE_WITH_VALUE or not value:
            return "approved as is"
        if "hours" in value:
            what = f"{value['hours']} h"
        elif "location" in value:
            what = f"Store {value['location']}"
        elif "leave" in value:
            return f"recorded as {value['leave']} leave"
        elif "amount" in value:
            what = labels.money(value["amount"])
        else:
            what = ", ".join(f"{k} {v}" for k, v in value.items())
        return f"approved with {what}"
