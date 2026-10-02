"""Exception codes, severities and builders.

Every condition the engine cannot settle with a rule ends up here so a human can review it.
"""
from __future__ import annotations

from datetime import date
from typing import Optional

from .types import PayrollException

INFO = "info"
NEEDS_REVIEW = "needs_review"
BLOCKING = "blocking"
SEVERITIES = (INFO, NEEDS_REVIEW, BLOCKING)

# code -> (default severity, short description)
CODES: dict[str, tuple[str, str]] = {
    # Imports (scenarios 1, 2)
    "FILE_UNREADABLE": (BLOCKING, "Time clock file could not be read"),
    "UNKNOWN_COLUMN": (INFO, "Unexpected column ignored"),
    # info only when the row cannot affect pay; the HR stage raises it to needs_review otherwise
    "MALFORMED_ROW": (INFO, "Row could not be parsed and was skipped"),
    "UNKNOWN_EMPLOYEE": (NEEDS_REVIEW, "Identifier not mapped to any employee"),
    "STORE_FILE_MISSING": (BLOCKING, "No time clock import for an expected store"),
    # Punch cleanup (scenarios 3-6)
    "DUPLICATE_PUNCH": (INFO, "Duplicate punch dropped"),
    "MISSED_CLOCK_OUT": (NEEDS_REVIEW, "Clock-in without clock-out"),
    "MISSED_CLOCK_IN": (NEEDS_REVIEW, "Clock-out without clock-in"),
    "ODD_PUNCH_COUNT": (NEEDS_REVIEW, "Odd number of punches, cannot pair"),
    "OVERNIGHT_SHIFT": (INFO, "Shift crosses midnight"),
    # Attendance (scenarios 7-9, 11)
    "LATE_ARRIVAL": (INFO, "Late arrival penalty applied"),
    "HALF_DAY": (INFO, "Half day deduction applied"),
    "ABSENCE_INFORMED": (INFO, "Absence covered by a leave record"),
    "ABSENCE_UNINFORMED": (NEEDS_REVIEW, "Absence without a leave record"),
    "UNKNOWN_LEAVE_TYPE": (NEEDS_REVIEW, "Leave type not in rules"),
    "WRONG_LOCATION": (NEEDS_REVIEW, "Shift punched at a different store than scheduled"),
    "UNSCHEDULED_SHIFT": (NEEDS_REVIEW, "Shift worked with no schedule"),
    "MISSING_RATE": (BLOCKING, "No pay rate for the store worked"),
    # Overtime (scenarios 10, 12, 14)
    "MULTI_LOCATION_OVERTIME": (INFO, "Weekly hours combined across stores for overtime"),
    "BLENDED_RATE_OVERTIME": (INFO, "Overtime premium on weighted average rate"),
    "ADDON_OT_RECALC": (INFO, "Overtime premium recalculated for an add-on"),
    # Finance (scenarios 13, 15, 16, 18)
    "LOAN_DEDUCTED": (INFO, "Loan/advance installment deducted"),
    "LOAN_CAPPED": (NEEDS_REVIEW, "Loan/advance deduction capped, remainder carried over"),
    "NEW_HIRE_PRORATED": (INFO, "Salary prorated from hire date"),
    "EXIT_FINAL_SETTLEMENT": (NEEDS_REVIEW, "Exit: final settlement needs review"),
    "CUSTOM_RULE_APPLIED": (INFO, "Custom deduction rule applied"),
    "NEGATIVE_NET": (BLOCKING, "Net pay is negative"),
}


def make(
    code: str,
    message: str,
    *,
    employee_id: Optional[str] = None,
    location_code: Optional[str] = None,
    on: Optional[date] = None,
    severity: Optional[str] = None,
    resolution: Optional[str] = None,
) -> PayrollException:
    default_severity, _ = CODES[code]
    severity = severity or default_severity
    return PayrollException(
        code=code,
        severity=severity,
        employee_id=employee_id,
        location_code=location_code,
        date=on,
        message=message,
        # info exceptions are settled by a rule; the resolution says which.
        auto_resolved=severity == INFO,
        resolution=resolution,
    )
