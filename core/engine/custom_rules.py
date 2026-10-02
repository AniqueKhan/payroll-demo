"""Company-specific deduction rules.

A small registry of rule types. Each rule is configured in the rules file and adds a
``CalcStep`` to the line it applies to. No tax or legal rule is hardcoded here: the
client supplies labels, rates and tables.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Callable

from . import exceptions as exc
from .explain import fmt_money, fmt_pct, money, to_decimal
from .types import PayrollException, PayrollLine

RuleFn = Callable[[dict, PayrollLine], Decimal]
REGISTRY: dict[str, tuple[RuleFn, Callable[[dict], None]]] = {}


def register(name: str, validator: Callable[[dict], None]):
    def wrap(fn: RuleFn) -> RuleFn:
        REGISTRY[name] = (fn, validator)
        return fn

    return wrap


def _basis(rule: dict, line: PayrollLine) -> Decimal:
    basis = rule.get("basis", "gross")
    if basis != "gross":
        raise ValueError(f"Unsupported basis {basis!r}")
    return line.gross


def _validate_percent(rule: dict) -> None:
    to_decimal(rule["rate_percent"])
    if rule.get("cap") is not None:
        to_decimal(rule["cap"])


def _validate_table(rule: dict) -> None:
    brackets = rule.get("brackets") or []
    if not brackets:
        raise ValueError(f"Rule {rule.get('label')!r} has no brackets")
    for b in brackets:
        if ("rate_percent" in b) == ("flat" in b):
            raise ValueError(f"Bracket {b} needs exactly one of rate_percent or flat")


@register("percent_of_gross", _validate_percent)
def percent_of_gross(rule: dict, line: PayrollLine) -> Decimal:
    basis = _basis(rule, line)
    rate = to_decimal(rule["rate_percent"])
    amount = money(basis * rate / 100)
    formula = f"{fmt_pct(rate)} x {fmt_money(basis)} gross = {fmt_money(amount)}"
    if rule.get("cap") is not None:
        cap = money(rule["cap"])
        if amount > cap:
            formula += f", capped at {fmt_money(cap)}"
            amount = cap
    line.step(rule["label"], formula, amount, rule="percent_of_gross")
    return amount


@register("table_lookup", _validate_table)
def table_lookup(rule: dict, line: PayrollLine) -> Decimal:
    basis = _basis(rule, line)
    for b in rule["brackets"]:
        lower = to_decimal(b["from"])
        upper = None if b.get("to") is None else to_decimal(b["to"])
        if basis >= lower and (upper is None or basis < upper):
            band = f"{fmt_money(lower)} to {'+' if upper is None else fmt_money(upper)}"
            if "flat" in b:
                amount = money(b["flat"])
                formula = f"gross {fmt_money(basis)} in bracket {band}: flat {fmt_money(amount)}"
            else:
                rate = to_decimal(b["rate_percent"])
                amount = money(basis * rate / 100)
                formula = f"gross {fmt_money(basis)} in bracket {band}: {fmt_pct(rate)} = {fmt_money(amount)}"
            line.step(rule["label"], formula, amount, rule="table_lookup")
            return amount
    line.step(rule["label"], f"gross {fmt_money(basis)} matches no bracket: $0.00", Decimal("0.00"))
    return Decimal("0.00")


def validate_rule(rule: dict) -> None:
    if rule.get("type") not in REGISTRY:
        raise ValueError(f"Unknown custom rule type {rule.get('type')!r}")
    if not rule.get("label"):
        raise ValueError("Custom rules need a label")
    REGISTRY[rule["type"]][1](rule)


def apply_custom_rules(rules_config: list[dict], lines: list[PayrollLine]) -> list[PayrollException]:
    """Apply each configured rule to every line, adding to ``custom_deductions``."""
    found: list[PayrollException] = []
    for rule in rules_config:
        fn, _ = REGISTRY[rule["type"]]
        total = Decimal("0.00")
        for line in lines:
            line.stage = "custom"
            if line.gross <= 0:
                line.step(rule["label"], "not applied: no gross pay this period", Decimal("0.00"))
                continue
            amount = fn(rule, line)
            line.custom_deductions += amount
            total += amount
        found.append(exc.make(
            "CUSTOM_RULE_APPLIED",
            f"{rule['label']} ({rule['type']}) applied to {sum(1 for x in lines if x.gross > 0)} employees, "
            f"total {fmt_money(total)}",
            resolution="Rate/table from rules file",
        ))
    return found
