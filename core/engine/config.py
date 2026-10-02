"""Load and validate the rules file into a typed object the stages read from."""
from __future__ import annotations

import copy
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

from .explain import to_decimal

WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


class RulesError(ValueError):
    pass


@dataclass
class Rules:
    raw: dict[str, Any]

    # Convenience accessors, all read from ``raw`` so the snapshot is the single source of truth.
    @property
    def period_length_days(self) -> int:
        return int(self.raw["period"]["length_days"])

    @property
    def week_start(self) -> int:
        return WEEKDAYS.index(self.raw["period"]["week_start"][:3].lower())

    @property
    def expected_locations(self) -> list[str]:
        return list(self.raw.get("expected_locations", []))

    @property
    def ot_threshold(self) -> Decimal:
        return to_decimal(self.raw["overtime"]["weekly_threshold_hours"])

    @property
    def ot_multiplier(self) -> Decimal:
        return to_decimal(self.raw["overtime"]["multiplier"])

    @property
    def ot_exempt_pay_types(self) -> list[str]:
        return list(self.raw["overtime"].get("exempt_pay_types", []))

    @property
    def duplicate_window_minutes(self) -> int:
        return int(self.raw["duplicate_punch_window_minutes"])

    @property
    def max_shift_hours(self) -> Decimal:
        return to_decimal(self.raw["punch_pairing"]["max_shift_hours"])

    @property
    def cluster_gap_hours(self) -> Decimal:
        return to_decimal(self.raw["punch_pairing"]["cluster_gap_hours"])

    @property
    def late_grace_minutes(self) -> int:
        return int(self.raw["late"]["grace_minutes"])

    @property
    def late_penalty(self) -> Decimal:
        return to_decimal(self.raw["late"]["penalty_per_late"])

    @property
    def half_day_threshold(self) -> Decimal:
        return to_decimal(self.raw["half_day"]["threshold_fraction"])

    @property
    def standard_workdays(self) -> set[int]:
        return {WEEKDAYS.index(d[:3].lower()) for d in self.raw["absence"]["standard_workdays"]}

    @property
    def uninformed_penalty(self) -> Decimal:
        return to_decimal(self.raw["absence"]["uninformed_penalty"])

    @property
    def leave_types(self) -> dict[str, bool]:
        return {lt["code"]: bool(lt["paid"]) for lt in self.raw.get("leave_types", [])}

    @property
    def loan_cap_percent(self) -> Decimal:
        return to_decimal(self.raw["loans"]["max_deduction_percent_of_net"])

    @property
    def final_pay_deducts_balance(self) -> bool:
        return bool(self.raw.get("final_pay", {}).get("deduct_remaining_loan_balance", False))

    @property
    def custom_rules(self) -> list[dict]:
        return list(self.raw.get("custom_rules", []))

    def with_changes(self, **dotted: Any) -> "Rules":
        """Return a copy with values replaced, e.g. ``with_changes(**{"late.penalty_per_late": "7.50"})``."""
        raw = copy.deepcopy(self.raw)
        for path, value in dotted.items():
            node = raw
            *parents, leaf = path.split(".")
            for key in parents:
                node = node[key]
            node[leaf] = value
        return Rules.from_dict(raw)

    @classmethod
    def from_dict(cls, raw: dict) -> "Rules":
        rules = cls(raw=raw)
        rules.validate()
        return rules

    def validate(self) -> None:
        required = ["period", "overtime", "duplicate_punch_window_minutes", "punch_pairing", "late",
                    "half_day", "absence", "leave_types", "loans"]
        missing = [k for k in required if k not in self.raw]
        if missing:
            raise RulesError(f"Rules file is missing sections: {', '.join(missing)}")
        if self.raw["overtime"].get("method") != "weighted_average":
            raise RulesError("Only the 'weighted_average' overtime method is supported")
        if self.period_length_days % 7:
            raise RulesError("Period length must be a whole number of weeks")
        from .custom_rules import validate_rule

        for rule in self.custom_rules:
            validate_rule(rule)


def load_rules(path: str | Path) -> Rules:
    with open(path, encoding="utf-8") as fh:
        return Rules.from_dict(yaml.safe_load(fh))


DEFAULT_RULES_PATH = Path(__file__).resolve().parent.parent / "rules" / "demo_rules.yaml"
