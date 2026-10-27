"""Plain-language labels shared by the screens and the Django admin."""
from __future__ import annotations

from datetime import date
from typing import Optional

from core.engine.explain import fmt_money

CODE_TITLES = {
    "FILE_UNREADABLE": "File could not be read",
    "UNKNOWN_COLUMN": "Unexpected column",
    "MALFORMED_ROW": "Unreadable row",
    "UNKNOWN_EMPLOYEE": "Unknown employee identifier",
    "STORE_FILE_MISSING": "Store file missing",
    "DUPLICATE_PUNCH": "Duplicate punch",
    "MISSED_CLOCK_OUT": "Missed clock-out",
    "MISSED_CLOCK_IN": "Missed clock-in",
    "ODD_PUNCH_COUNT": "Odd number of punches",
    "OVERNIGHT_SHIFT": "Overnight shift",
    "LATE_ARRIVAL": "Late arrival",
    "HALF_DAY": "Half day",
    "ABSENCE_INFORMED": "Absence with leave record",
    "ABSENCE_UNINFORMED": "Uninformed absence",
    "UNKNOWN_LEAVE_TYPE": "Unknown leave type",
    "WRONG_LOCATION": "Shift logged at the wrong store",
    "UNSCHEDULED_SHIFT": "Unscheduled shift",
    "MISSING_RATE": "Missing pay rate",
    "MULTI_LOCATION_OVERTIME": "Overtime across two stores",
    "BLENDED_RATE_OVERTIME": "Overtime on a blended rate",
    "ADDON_OT_RECALC": "Overtime recalculated for an add-on",
    "LOAN_DEDUCTED": "Loan or advance deducted",
    "LOAN_CAPPED": "Loan deduction capped",
    "NEW_HIRE_PRORATED": "New hire prorated",
    "EXIT_FINAL_SETTLEMENT": "Final settlement on exit",
    "CUSTOM_RULE_APPLIED": "Custom deduction applied",
    "NEGATIVE_NET": "Negative net pay",
}

SEVERITY_LABELS = {"blocking": "Blocking", "needs_review": "Needs review", "info": "Handled by rule"}


def code_title(code: str) -> str:
    return CODE_TITLES.get(code, code.replace("_", " ").capitalize())


def period_label(start: date, end: date, with_year: bool = False) -> str:
    """'Sep 7 to 20', or 'Sep 28 to Oct 11' when the period spans two months; optionally ', 2026'."""
    tail = f"{end.day}" if (start.year, start.month) == (end.year, end.month) else f"{end:%b} {end.day}"
    label = f"{start:%b} {start.day} to {tail}"
    return f"{label}, {end.year}" if with_year else label


def short_date(d: Optional[date]) -> str:
    """'Sep 14'."""
    return f"{d:%b} {d.day}" if d else ""


def day_date(d: date) -> str:
    """'Wed Sep 10'."""
    return f"{d:%a %b} {d.day}"


def money(value) -> str:
    return fmt_money(value)
