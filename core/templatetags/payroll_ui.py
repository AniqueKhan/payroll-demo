"""Formatting helpers for the demo screens."""
from decimal import Decimal, InvalidOperation

from django import template

from core.engine.explain import fmt_money

register = template.Library()

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


def _dec(value):
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


@register.filter
def money(value):
    d = _dec(value)
    return "" if d is None else fmt_money(d)


@register.filter
def hours(value):
    d = _dec(value)
    return "" if d is None else f"{d.quantize(Decimal('0.01')):,}"


@register.filter
def code_title(code):
    return CODE_TITLES.get(code, code.replace("_", " ").capitalize())


@register.filter
def severity_label(severity):
    return SEVERITY_LABELS.get(severity, severity)


@register.filter
def get(mapping, key):
    try:
        return mapping.get(key)
    except AttributeError:
        return None
