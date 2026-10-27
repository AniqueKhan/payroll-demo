"""Load and validate the rules file into a typed object the stages read from."""
from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from datetime import time
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
    def week_start_time(self) -> time:
        """When each workweek starts on ``week_start`` day, for counting overtime. Validated in ``validate``."""
        hours, minutes = str(self.raw.get("week_start_time", "00:00")).split(":")
        return time(int(hours), int(minutes))

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
    def salaried_partial_day_deduction(self) -> bool:
        """Deduct half a day from exempt salaried pay for a partial day (off by default)."""
        return bool(self.raw.get("salaried_partial_day_deduction", False))

    @property
    def standard_workdays(self) -> set[int]:
        return {WEEKDAYS.index(d[:3].lower()) for d in self.raw["absence"]["standard_workdays"]}

    @property
    def attendance_penalties_enabled(self) -> bool:
        """Late and uninformed-absence penalties are only charged when switched on (off by default)."""
        return bool(self.raw.get("attendance_penalties_enabled", False))

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
        start = self.raw.get("week_start_time", "00:00")
        m = re.fullmatch(r"(\d{2}):(\d{2})", start) if isinstance(start, str) else None
        if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
            raise RulesError(f"week_start_time must be a time of day between \"00:00\" and \"23:59\", written "
                             f"HH:MM in quotes (for example \"06:00\"), not {start!r}")
        from .custom_rules import validate_rule

        for rule in self.custom_rules:
            validate_rule(rule)


def load_rules(path: str | Path) -> Rules:
    with open(path, encoding="utf-8") as fh:
        return Rules.from_dict(yaml.safe_load(fh))


DEFAULT_RULES_PATH = Path(__file__).resolve().parent.parent / "rules" / "demo_rules.yaml"
