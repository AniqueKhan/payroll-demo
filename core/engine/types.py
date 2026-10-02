"""Engine data types. Pure Python: nothing in core.engine imports Django."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Literal, Optional

PayType = Literal["hourly", "salaried"]
PunchKind = Optional[Literal["in", "out"]]
Severity = Literal["info", "needs_review", "blocking"]


@dataclass(frozen=True)
class Location:
    code: str
    name: str
    timezone: str
    export_format: str


@dataclass(frozen=True)
class Employee:
    id: str
    name: str
    home_location: str
    pay_type: PayType
    rates: dict[str, Decimal]
    salary_per_period: Optional[Decimal]
    hire_date: date
    exit_date: Optional[date]
    external_ids: dict[str, str]


@dataclass(frozen=True)
class ScheduledShift:
    employee_id: str
    location_code: str
    date: date
    start: datetime
    end: datetime  # may fall on the next day for overnight shifts

    @property
    def hours(self) -> Decimal:
        return minutes_to_hours((self.end - self.start).total_seconds() / 60)


@dataclass(frozen=True)
class Punch:
    employee_id: str
    location_code: str
    timestamp: datetime
    kind: PunchKind
    source_row: int


@dataclass
class Shift:
    employee_id: str
    location_code: str  # where the punches came from
    date: date  # attributed to the start date
    start: Optional[datetime]
    end: Optional[datetime]
    hours: Decimal
    flags: list[str] = field(default_factory=list)
    pay_location_code: Optional[str] = None  # set when pay/cost goes to another store
    raw_punches: list[Punch] = field(default_factory=list)

    @property
    def paid_at(self) -> str:
        return self.pay_location_code or self.location_code


@dataclass(frozen=True)
class LeaveRecord:
    employee_id: str
    date: date
    leave_type: str
    paid: bool
    informed: bool


@dataclass(frozen=True)
class PayAddOn:
    employee_id: str
    date: date
    kind: Literal["commission", "bonus"]
    amount: Decimal
    include_in_regular_rate: bool


@dataclass(frozen=True)
class LoanAdvance:
    employee_id: str
    kind: Literal["loan", "advance"]
    total: Decimal
    installment: Decimal
    start_period: date
    paid_to_date: Decimal
    ref: Optional[str] = None  # caller's identifier, echoed into the trail

    @property
    def remaining(self) -> Decimal:
        return self.total - self.paid_to_date


STAGES = ("hr", "finance", "custom")


@dataclass(frozen=True)
class CalcStep:
    label: str
    formula: str
    amount: Optional[Decimal]
    refs: dict = field(default_factory=dict)
    stage: str = "hr"  # hr | finance | custom

    @property
    def is_adjustment(self) -> bool:
        return self.label.startswith(ADJUSTMENT_LABEL)

    def to_dict(self) -> dict:
        return {
            "label": self.label,
            "formula": self.formula,
            "amount": None if self.amount is None else str(self.amount),
            "refs": self.refs,
            "stage": self.stage,
        }


ADJUSTMENT_LABEL = "Reviewer adjustment"


ZERO = Decimal("0.00")


@dataclass
class PayrollLine:
    employee_id: str
    regular_pay: Decimal = ZERO
    overtime_pay: Decimal = ZERO
    addons: Decimal = ZERO
    penalties: Decimal = ZERO
    absence_deductions: Decimal = ZERO
    loan_deductions: Decimal = ZERO
    custom_deductions: Decimal = ZERO
    gross: Decimal = ZERO
    net: Decimal = ZERO
    by_location: dict = field(default_factory=dict)
    trail: list[CalcStep] = field(default_factory=list)
    hours: Decimal = ZERO  # hours worked and paid (leave excluded)
    ot_hours: Decimal = ZERO
    stage: str = "hr"  # stage that new trail steps are filed under

    def step(self, label: str, formula: str, amount: Optional[Decimal] = None, **refs) -> None:
        self.trail.append(CalcStep(label, formula, amount, {k: str(v) for k, v in refs.items()}, self.stage))

    def adjustment(self, what: str, formula: str, note: str, amount: Optional[Decimal] = None, **refs) -> None:
        """Trail step recording that a reviewer changed the engine's default."""
        note = f" (note: {note})" if note else ""
        self.step(f"{ADJUSTMENT_LABEL}: {what}", f"{formula}{note}", amount, adjustment=1, **refs)


@dataclass
class PayrollException:
    code: str
    severity: Severity
    employee_id: Optional[str]
    location_code: Optional[str]
    date: Optional[date]
    message: str
    auto_resolved: bool = False
    resolution: Optional[str] = None
    rows: list[tuple[str, int]] = field(default_factory=list)  # (location_code, source_row) involved
    context: dict = field(default_factory=dict)  # structured facts for reviewers (suggested hours, ...)
    adjusted: bool = False  # a reviewer adjustment was applied for this exception

    @property
    def key(self) -> str:
        return exception_key(self.code, self.employee_id, self.date, self.location_code)


def exception_key(code: str, employee_id: Optional[str], on: Optional[date], location_code: Optional[str]) -> str:
    """Stable identity of an exception across re-runs: ``code|employee_id|date|location_code``."""
    return "|".join([code, employee_id or "", on.isoformat() if on else "", location_code or ""])


def parse_exception_key(key: str) -> tuple[str, Optional[str], Optional[date], Optional[str]]:
    code, employee_id, on, loc = key.split("|")
    return code, employee_id or None, date.fromisoformat(on) if on else None, loc or None


# ---------------------------------------------------------------- reviewer adjustments

@dataclass(frozen=True)
class HoursOverride:
    employee_id: str
    date: date
    location_code: str
    hours: Decimal
    note: str = ""


@dataclass(frozen=True)
class LocationOverride:
    employee_id: str
    date: date
    location_code: str  # store to pay at and allocate cost to
    note: str = ""


@dataclass(frozen=True)
class LeaveOverride:
    employee_id: str
    date: date
    leave_type: str
    note: str = ""


@dataclass(frozen=True)
class LoanOverride:
    employee_id: str
    loan_kind: str
    amount: Decimal
    note: str = ""


@dataclass
class Adjustments:
    """Reviewer decisions that replace the engine's default handling for one employee and date."""
    hours_overrides: list[HoursOverride] = field(default_factory=list)
    location_overrides: list[LocationOverride] = field(default_factory=list)
    leave_overrides: list[LeaveOverride] = field(default_factory=list)
    loan_overrides: list[LoanOverride] = field(default_factory=list)

    def hours_for(self, employee_id: str, day: date, loc: str) -> Optional[HoursOverride]:
        return next((o for o in self.hours_overrides
                     if (o.employee_id, o.date, o.location_code) == (employee_id, day, loc)), None)

    def location_for(self, employee_id: str, day: date) -> Optional[LocationOverride]:
        return next((o for o in self.location_overrides if (o.employee_id, o.date) == (employee_id, day)), None)

    def leave_for(self, employee_id: str, day: date) -> Optional[LeaveOverride]:
        return next((o for o in self.leave_overrides if (o.employee_id, o.date) == (employee_id, day)), None)

    def loan_for(self, employee_id: str, kind: str) -> Optional[LoanOverride]:
        return next((o for o in self.loan_overrides if (o.employee_id, o.loan_kind) == (employee_id, kind)), None)


@dataclass(frozen=True)
class Period:
    start: date
    end: date


@dataclass
class PayrollResult:
    lines: list[PayrollLine]
    exceptions: list[PayrollException]
    period: Period
    totals: dict

    def line_for(self, employee_id: str) -> PayrollLine:
        return next(line for line in self.lines if line.employee_id == employee_id)

    def exceptions_for(self, code: Optional[str] = None, employee_id: Optional[str] = None) -> list[PayrollException]:
        return [
            e for e in self.exceptions
            if (code is None or e.code == code) and (employee_id is None or e.employee_id == employee_id)
        ]


def minutes_to_hours(minutes: float) -> Decimal:
    from .explain import round_hours

    return round_hours(Decimal(int(round(minutes))) / Decimal(60))
