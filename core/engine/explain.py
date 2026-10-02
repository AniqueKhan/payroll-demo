"""Rounding, formatting and allocation helpers used to build calculation trails."""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Mapping

CENT = Decimal("0.01")


def to_decimal(value) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        return Decimal(str(value))
    return Decimal(value)


def money(value) -> Decimal:
    """Round half up to cents."""
    return to_decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


def round_hours(value) -> Decimal:
    return to_decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


def fmt_money(value) -> str:
    value = money(value)
    sign = "-" if value < 0 else ""
    return f"{sign}${abs(value):,.2f}"


def fmt_hours(value) -> str:
    return f"{round_hours(value)}"


def fmt_pct(value) -> str:
    return f"{to_decimal(value).normalize():f}%"


def allocate(amount: Decimal, weights: Mapping[str, Decimal]) -> dict[str, Decimal]:
    """Split ``amount`` across keys pro rata to ``weights``; rounding remainder goes to the largest weight."""
    amount = money(amount)
    total = sum(weights.values(), Decimal(0))
    if not weights:
        return {}
    if total == 0:
        first = next(iter(weights))
        return {k: (amount if k == first else Decimal("0.00")) for k in weights}
    shares = {k: money(amount * w / total) for k, w in weights.items()}
    remainder = amount - sum(shares.values(), Decimal(0))
    if remainder:
        biggest = max(weights, key=lambda k: weights[k])
        shares[biggest] += remainder
    return shares
