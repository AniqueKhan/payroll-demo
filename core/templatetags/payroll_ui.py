"""Formatting helpers for the demo screens."""
from decimal import Decimal, InvalidOperation

from django import template

from core import labels
from core.engine.explain import fmt_money
from core.labels import SEVERITY_LABELS

register = template.Library()


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
    return labels.code_title(code)


@register.filter
def severity_label(severity):
    return SEVERITY_LABELS.get(severity, severity)


@register.filter
def get(mapping, key):
    try:
        return mapping.get(key)
    except AttributeError:
        return None
