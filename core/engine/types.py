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


@dataclass(frozen=True)
class CalcStep:
    label: str
    formula: str
    amount: Optional[Decimal]
    refs: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "label": self.label,
            "formula": self.formula,
            "amount": None if self.amount is None else str(self.amount),
            "refs": self.refs,
        }


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

    def step(self, label: str, formula: str, amount: Optional[Decimal] = None, **refs) -> None:
        self.trail.append(CalcStep(label, formula, amount, {k: str(v) for k, v in refs.items()}))


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
