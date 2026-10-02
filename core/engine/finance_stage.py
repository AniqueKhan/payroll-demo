"""Finance stage: add-ons, gross pay, loans/advances and final pay."""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from typing import Iterable

from . import exceptions as exc
from .config import Rules
from .explain import fmt_hours, fmt_money, fmt_pct, money
from .hr_stage import EmployeeHR, bump, compute_premium
from .types import ZERO, LoanAdvance, PayAddOn, PayrollException, Period


def apply_addons(hr: EmployeeHR, addons: list[PayAddOn], rules: Rules) -> list[PayrollException]:
    found: list[PayrollException] = []
    e, line = hr.employee, hr.line
    included_by_week: dict[int, Decimal] = defaultdict(lambda: ZERO)
    for a in sorted(addons, key=lambda a: a.date):
        amount = money(a.amount)
        line.addons += amount
        bump(line, e.home_location, "addons", amount)
        note = "included in regular rate" if a.include_in_regular_rate else "not in regular rate"
        line.step(f"{a.kind.title()} {a.date}", f"{fmt_money(amount)} ({note})", amount, date=a.date)
        if a.include_in_regular_rate and e.pay_type == "hourly":
            week = next((w for w in hr.weeks if w.contains(a.date)), None)
            if week is not None:
                included_by_week[week.index] += amount

    for week in hr.weeks:
        extra = included_by_week.get(week.index)
        if not extra or not week.ot_hours:
            continue
        old_rate, old_premium = week.weighted_rate, week.premium
        change = compute_premium(week, line, rules, label=f"Week {week.index} OT premium (recalculated)",
                                 extra_regular=extra)
        found.append(exc.make(
            "ADDON_OT_RECALC",
            f"Week {week.index}: {fmt_money(extra)} add-on in regular rate raised weighted rate "
            f"{fmt_money(old_rate)} -> {fmt_money(week.weighted_rate)}; OT premium on {fmt_hours(week.ot_hours)} h "
            f"{fmt_money(old_premium)} -> {fmt_money(week.premium)} (+{fmt_money(change)})",
            employee_id=e.id, on=week.start, resolution="OT premium recalculated",
        ))
    return found


def compute_gross(hr: EmployeeHR) -> None:
    line = hr.line
    line.gross = line.regular_pay + line.overtime_pay + line.addons - line.absence_deductions
    line.step("Gross pay", f"regular {fmt_money(line.regular_pay)} + OT {fmt_money(line.overtime_pay)} + add-ons "
              f"{fmt_money(line.addons)} - absence {fmt_money(line.absence_deductions)} = {fmt_money(line.gross)}",
              line.gross)
    for bucket in line.by_location.values():
        bucket["gross"] = (bucket["regular_pay"] + bucket["overtime_pay"] + bucket["addons"]
                           - bucket["absence_deductions"])


def apply_loans(hr: EmployeeHR, loans: list[LoanAdvance], period: Period, rules: Rules) -> list[PayrollException]:
    found: list[PayrollException] = []
    e, line = hr.employee, hr.line
    exiting = e.exit_date is not None and period.start <= e.exit_date <= period.end
    net_before = line.gross - line.penalties
    cap = max(money(net_before * rules.loan_cap_percent / 100), ZERO)
    cap_left = cap
    active = [ln for ln in loans if ln.start_period <= period.start and ln.remaining > 0]
    if active:
        line.step("Loan/advance cap", f"{fmt_pct(rules.loan_cap_percent)} x net before loans "
                  f"{fmt_money(net_before)} = {fmt_money(cap)}", cap)
    unsettled = ZERO
    for ln in sorted(active, key=lambda ln: ln.start_period):
        remaining = money(ln.remaining)
        final = exiting and rules.final_pay_deducts_balance
        due = remaining if final else min(money(ln.installment), remaining)
        take = min(due, cap_left)
        cap_left -= take
        line.loan_deductions += take
        after = remaining - take
        label = f"{ln.kind.title()} {'final balance' if final else 'installment'}"
        line.step(label, f"{fmt_money(take)} (balance after: {fmt_money(after)})", take, kind=ln.kind,
                  loan=ln.ref or "")
        if final:
            unsettled += after
        elif take < due:
            carry = due - take
            found.append(exc.make(
                "LOAN_CAPPED",
                f"{ln.kind.title()} installment {fmt_money(due)} exceeds cap {fmt_money(cap)} "
                f"({fmt_pct(rules.loan_cap_percent)} of {fmt_money(net_before)}); deducted {fmt_money(take)}, "
                f"{fmt_money(carry)} carried to next period (balance after: {fmt_money(after)})",
                employee_id=e.id, on=period.end,
            ))
        else:
            found.append(exc.make(
                "LOAN_DEDUCTED",
                f"{ln.kind.title()} installment {fmt_money(take)} deducted (balance after: {fmt_money(after)})",
                employee_id=e.id, on=period.end, resolution="Installment from loans_advances.csv",
            ))
    if exiting:
        msg = f"Exit {e.exit_date}: salary prorated to exit date" if e.pay_type == "salaried" \
            else f"Exit {e.exit_date}: paid hours worked to exit date"
        if active:
            msg += f"; loan balance deducted {fmt_money(line.loan_deductions)} (cap {fmt_money(cap)})"
        if unsettled:
            msg += f"; {fmt_money(unsettled)} still owed, settle outside payroll"
        found.append(exc.make("EXIT_FINAL_SETTLEMENT", msg, employee_id=e.id, on=e.exit_date))
    return found


def run_finance_stage(
    hr: dict[str, EmployeeHR],
    addons: Iterable[PayAddOn],
    loans: Iterable[LoanAdvance],
    period: Period,
    rules: Rules,
) -> list[PayrollException]:
    found: list[PayrollException] = []
    addons_by_emp: dict[str, list[PayAddOn]] = defaultdict(list)
    for a in addons:
        if period.start <= a.date <= period.end:
            addons_by_emp[a.employee_id].append(a)
    loans_by_emp: dict[str, list[LoanAdvance]] = defaultdict(list)
    for ln in loans:
        loans_by_emp[ln.employee_id].append(ln)

    for emp_id, emp_hr in hr.items():
        found.extend(apply_addons(emp_hr, addons_by_emp[emp_id], rules))
        compute_gross(emp_hr)
        found.extend(apply_loans(emp_hr, loans_by_emp[emp_id], period, rules))
    return found
