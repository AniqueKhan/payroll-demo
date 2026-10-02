import uuid
from pathlib import Path

from django.db import models
from django.db.models import Q
from django.utils import timezone


class Location(models.Model):
    code = models.CharField(max_length=10, unique=True)
    name = models.CharField(max_length=100)
    timezone = models.CharField(max_length=64)
    export_format = models.CharField(max_length=32)

    class Meta:
        ordering = ["code"]

    def __str__(self):
        return f"{self.code} - {self.name}"


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
        return f"{self.start_date} to {self.end_date}"


class ScheduledShift(models.Model):
    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name="schedules")
    location = models.ForeignKey(Location, on_delete=models.PROTECT)
    date = models.DateField()
    start = models.TimeField()
    end = models.TimeField()  # earlier than start means the shift ends the next day

    class Meta:
        ordering = ["date", "start"]


class LeaveRecord(models.Model):
    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name="leaves")
    date = models.DateField()
    leave_type = models.CharField(max_length=32)
    paid = models.BooleanField()
    informed = models.BooleanField(default=True)


class PayAddOn(models.Model):
    KINDS = [("commission", "Commission"), ("bonus", "Bonus")]

    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name="addons")
    date = models.DateField()
    kind = models.CharField(max_length=20, choices=KINDS)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    include_in_regular_rate = models.BooleanField(default=False)


class LoanAdvance(models.Model):
    KINDS = [("loan", "Loan"), ("advance", "Advance")]

    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name="loans")
    kind = models.CharField(max_length=10, choices=KINDS)
    total = models.DecimalField(max_digits=10, decimal_places=2)
    installment = models.DecimalField(max_digits=10, decimal_places=2)
    start_period = models.DateField()
    paid_to_date = models.DecimalField(max_digits=10, decimal_places=2, default=0)


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

    @property
    def basename(self) -> str:
        return Path(self.filename).name


class RawImportRow(models.Model):
    """One row of a store export, kept exactly as it arrived so the UI can show the 'before'."""
    batch = models.ForeignKey(ImportBatch, on_delete=models.CASCADE, related_name="raw_rows")
    row_number = models.PositiveIntegerField()  # line number in the file (header is 1)
    raw = models.JSONField()  # {column: value as exported}
    parse_error = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["batch", "row_number"]
        constraints = [models.UniqueConstraint(fields=["batch", "row_number"], name="unique_raw_row")]


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
        return f"Sandbox {self.id}"


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
        where = f"sandbox {self.sandbox_id}" if self.sandbox_id else "baseline"
        return f"Run {self.pk} {self.period} ({self.status}, {where})"


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
    net = models.DecimalField(max_digits=12, decimal_places=2)
    hours = models.DecimalField(max_digits=8, decimal_places=2, default=0)
    overtime_hours = models.DecimalField(max_digits=8, decimal_places=2, default=0)
    by_location = models.JSONField(default=dict)
    trail = models.JSONField(default=list)

    class Meta:
        ordering = ["employee__employee_id"]

    @property
    def regular_hours(self):
        return self.hours - self.overtime_hours

    @property
    def deductions(self):
        return self.penalties + self.loan_deductions + self.custom_deductions


class PayrollExceptionRecord(models.Model):
    SEVERITIES = [("info", "Info"), ("needs_review", "Needs review"), ("blocking", "Blocking")]
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
        return f"{self.severity} {self.code}"


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
        return f"{self.exception_key}: {self.action}"
