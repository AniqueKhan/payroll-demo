"""Formatting helpers for the demo screens."""
from decimal import Decimal, InvalidOperation

from django import template
from django.utils.html import escape
from django.utils.safestring import mark_safe

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


@register.filter
def mark_chars(value, positions):
    """Wrap the characters at ``positions`` in <mark>, everything escaped: '9:6O' -> '9:<mark>6O</mark>'."""
    marks = set(positions or [])
    out, open_ = [], False
    for i, c in enumerate(str(value)):
        if (i in marks) != open_:
            out.append("<mark>" if not open_ else "</mark>")
            open_ = not open_
        out.append(escape(c))
    if open_:
        out.append("</mark>")
    return mark_safe("".join(out))
