"""HR stage: punches -> shifts -> hours, lates, half days, absences, overtime.

Produces one partially filled ``PayrollLine`` per employee (earnings, attendance
deductions and penalties) plus the weekly hour buckets the finance stage needs to
recalculate overtime when an add-on raises the regular rate.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Iterable, Optional

from . import exceptions as exc
from .config import Rules
from .explain import allocate, fmt_hours, fmt_money, money
from .types import (
    ZERO, Employee, LeaveRecord, PayrollException, PayrollLine, Period, Punch,
    ScheduledShift, Shift, minutes_to_hours,
)

LOCATION_FIELDS = ("hours", "regular_pay", "overtime_pay", "addons", "absence_deductions", "gross")


@dataclass
class WeekHours:
    index: int
    start: date
    hours_by_loc: dict[str, Decimal] = field(default_factory=dict)
    straight_by_loc: dict[str, Decimal] = field(default_factory=dict)
    ot_hours: Decimal = ZERO
    weighted_rate: Optional[Decimal] = None
    premium: Decimal = ZERO

    @property
    def total_hours(self) -> Decimal:
        return sum(self.hours_by_loc.values(), ZERO)

    @property
    def straight_total(self) -> Decimal:
        return sum(self.straight_by_loc.values(), ZERO)

    def contains(self, day: date) -> bool:
        return self.start <= day < self.start + timedelta(days=7)


@dataclass
class EmployeeHR:
    employee: Employee
    line: PayrollLine
    weeks: list[WeekHours]
    shifts: list[Shift]
    daily_rate: Optional[Decimal] = None


def bump(line: PayrollLine, loc: str, key: str, amount: Decimal) -> None:
    bucket = line.by_location.setdefault(loc, {k: ZERO for k in LOCATION_FIELDS})
    bucket[key] += amount


def store(code: str) -> str:
    return f"Store {code}"


def hhmm(dt) -> str:
    return dt.strftime("%H:%M") if dt else "--:--"


# ---------------------------------------------------------------- punch cleanup

def drop_duplicates(punches: list[Punch], window_minutes: int) -> tuple[list[Punch], list[PayrollException]]:
    """Same kind (or any, when the export has no kind) within the window: keep the first."""
    kept: list[Punch] = []
    found: list[PayrollException] = []
    window = timedelta(minutes=window_minutes)
    groups: dict[tuple, list[Punch]] = defaultdict(list)
    for p in punches:
        groups[(p.employee_id, p.location_code)].append(p)
    for group in groups.values():
        group.sort(key=lambda p: (p.timestamp, p.source_row))
        for p in group:
            prev = kept[-1] if kept and kept[-1].employee_id == p.employee_id \
                and kept[-1].location_code == p.location_code else None
            if prev and prev.kind == p.kind and p.timestamp - prev.timestamp <= window:
                found.append(exc.make(
                    "DUPLICATE_PUNCH",
                    f"{(p.kind or 'punch').upper()} at {hhmm(p.timestamp)} (row {p.source_row}) repeats "
                    f"{hhmm(prev.timestamp)} (row {prev.source_row}) within {window_minutes} min",
                    employee_id=p.employee_id, location_code=p.location_code, on=p.timestamp.date(),
                    resolution=f"Kept first punch (row {prev.source_row}), dropped row {p.source_row}",
                ))
                continue
            kept.append(p)
    return kept, found


def _shift(punches: list[Punch], start: Punch, end: Optional[Punch], flags=None) -> Shift:
    hours = minutes_to_hours((end.timestamp - start.timestamp).total_seconds() / 60) if end else ZERO
    s = Shift(start.employee_id, start.location_code, start.timestamp.date(), start.timestamp,
              end.timestamp if end else None, hours, list(flags or []), raw_punches=punches)
    if end and end.timestamp.date() != start.timestamp.date():
        s.flags.append("overnight")
    return s


def pair_with_kinds(group: list[Punch], max_hours: Decimal) -> list[Shift]:
    shifts: list[Shift] = []
    open_in: Optional[Punch] = None
    limit = timedelta(hours=float(max_hours))
    for p in group:
        if p.kind == "in":
            if open_in:
                shifts.append(_shift([open_in], open_in, None, ["missed_clock_out"]))
            open_in = p
        else:
            if open_in and p.timestamp - open_in.timestamp <= limit:
                shifts.append(_shift([open_in, p], open_in, p))
            else:
                if open_in:
                    shifts.append(_shift([open_in], open_in, None, ["missed_clock_out"]))
                shifts.append(Shift(p.employee_id, p.location_code, p.timestamp.date(), None, p.timestamp,
                                    ZERO, ["missed_clock_in"], raw_punches=[p]))
            open_in = None
    if open_in:
        shifts.append(_shift([open_in], open_in, None, ["missed_clock_out"]))
    return shifts


def pair_alternating(group: list[Punch], gap_hours: Decimal) -> list[Shift]:
    """Exports without IN/OUT: cluster punches into work days, then pair in order."""
    clusters: list[list[Punch]] = []
    gap = timedelta(hours=float(gap_hours))
    for p in group:
        if clusters and p.timestamp - clusters[-1][-1].timestamp < gap:
            clusters[-1].append(p)
        else:
            clusters.append([p])
    shifts: list[Shift] = []
    for cluster in clusters:
        if len(cluster) % 2:
            first = cluster[0]
            shifts.append(Shift(first.employee_id, first.location_code, first.timestamp.date(), first.timestamp,
                                None, ZERO, ["odd_punches"], raw_punches=cluster))
            continue
        for i in range(0, len(cluster), 2):
            shifts.append(_shift(cluster[i:i + 2], cluster[i], cluster[i + 1]))
    return shifts


def build_shifts(punches: list[Punch], rules: Rules) -> list[Shift]:
    groups: dict[tuple, list[Punch]] = defaultdict(list)
    for p in punches:
        groups[(p.employee_id, p.location_code)].append(p)
    shifts: list[Shift] = []
    for group in groups.values():
        group.sort(key=lambda p: (p.timestamp, p.source_row))
        if any(p.kind is None for p in group):
            shifts.extend(pair_alternating(group, rules.cluster_gap_hours))
        else:
            shifts.extend(pair_with_kinds(group, rules.max_shift_hours))
    return shifts


# ---------------------------------------------------------------- per employee

def employed_on(e: Employee, day: date) -> bool:
    return e.hire_date <= day and (e.exit_date is None or day <= e.exit_date)


def standard_workdays(period: Period, rules: Rules) -> int:
    days = (period.end - period.start).days + 1
    return sum(1 for i in range(days) if (period.start + timedelta(days=i)).weekday() in rules.standard_workdays)


class _EmployeeRun:
    def __init__(self, e: Employee, period: Period, rules: Rules, schedules: list[ScheduledShift],
                 shifts: list[Shift], leaves: dict[date, LeaveRecord], missing_locations: set[str],
                 bad_rows: Optional[dict[tuple[str, date], list[PayrollException]]] = None):
        self.e, self.period, self.rules = e, period, rules
        self.bad_rows = bad_rows or {}
        self.schedules = sorted(schedules, key=lambda s: s.start)
        self.shifts = sorted(shifts, key=lambda s: (s.date, s.start or s.end))
        self.leaves = leaves
        self.missing = missing_locations
        self.line = PayrollLine(employee_id=e.id)
        self.found: list[PayrollException] = []
        self.hourly = e.pay_type == "hourly"
        self.daily_rate: Optional[Decimal] = None
        n_weeks = rules.period_length_days // 7
        self.weeks = [WeekHours(i + 1, period.start + timedelta(days=7 * i)) for i in range(n_weeks)]

    def flag(self, code, message, *, loc=None, on=None, resolution=None, severity=None):
        self.found.append(exc.make(code, message, employee_id=self.e.id, location_code=loc, on=on,
                                   resolution=resolution, severity=severity))

    def sched_on(self, day: date) -> list[ScheduledShift]:
        return [s for s in self.schedules if s.date == day]

    def rate(self, loc: str) -> Decimal:
        if loc not in self.e.rates:
            return None
        return self.e.rates[loc]

    # -- steps

    def run(self) -> EmployeeHR:
        if not self.hourly:
            self.salary_base()
        self.review_shifts()
        self.attendance()
        if self.hourly:
            self.weekly_hours()
        return EmployeeHR(self.e, self.line, self.weeks, self.shifts, self.daily_rate)

    def salary_base(self) -> None:
        e, line = self.e, self.line
        days = standard_workdays(self.period, self.rules)
        self.daily_rate = money(e.salary_per_period / days)
        line.step("Daily rate", f"{fmt_money(e.salary_per_period)} / {days} scheduled working days = "
                  f"{fmt_money(self.daily_rate)}", self.daily_rate)
        hired_mid = e.hire_date > self.period.start
        exits_mid = e.exit_date is not None and e.exit_date < self.period.end
        if hired_mid or exits_mid:
            window_start = max(self.period.start, e.hire_date)
            window_end = min(self.period.end, e.exit_date or self.period.end)
            worked_days = sorted({s.date for s in self.schedules if window_start <= s.date <= window_end})
            salary = money(self.daily_rate * len(worked_days))
            why = []
            if hired_mid:
                why.append(f"hired {e.hire_date}")
            if exits_mid:
                why.append(f"exit {e.exit_date}")
            line.step("Salary (prorated)", f"{fmt_money(self.daily_rate)} x {len(worked_days)} scheduled working "
                      f"days {window_start} to {window_end} ({', '.join(why)}) = {fmt_money(salary)}", salary,
                      days=len(worked_days))
            if hired_mid:
                self.flag("NEW_HIRE_PRORATED", f"Hired {e.hire_date}: salary prorated to {len(worked_days)} "
                          f"scheduled working days = {fmt_money(salary)}", on=e.hire_date,
                          resolution="Prorated by scheduled working days")
        else:
            salary = money(e.salary_per_period)
            line.step("Salary", f"{fmt_money(salary)} (full period)", salary)
        line.regular_pay += salary
        bump(line, e.home_location, "regular_pay", salary)

    def review_shifts(self) -> None:
        """Flag unresolved shifts, overnight shifts and store mismatches; set the pay location."""
        for s in self.shifts:
            sched = self.sched_on(s.date)
            hint = (f"Scheduled {hhmm(sched[0].start)}-{hhmm(sched[0].end)} ({fmt_hours(sched[0].hours)} h): "
                    f"suggested {fmt_hours(sched[0].hours)} h") if sched else "No schedule to suggest hours from"
            if "malformed_row" in s.flags:
                bad = "; ".join(x.message for x in self.bad_rows[(s.location_code, s.date)])
                raw = ", ".join(f"{p.timestamp:%H:%M} {(p.kind or 'punch').upper()} (row {p.source_row})"
                                for p in s.raw_punches) or "none"
                self.flag("MALFORMED_ROW", f"{bad}. The remaining punches cannot be paired: counted "
                          f"as 0.00 h. Remaining punches: {raw}. {hint}", loc=s.location_code, on=s.date,
                          severity=exc.NEEDS_REVIEW)
                self.line.step(f"{s.date} {store(s.location_code)}", f"unreadable punch row; remaining punches "
                               f"{raw}: 0.00 h pending review", None, date=s.date, step="shift")
                continue
            if "missed_clock_out" in s.flags:
                self.flag("MISSED_CLOCK_OUT", f"IN at {hhmm(s.start)} at {store(s.location_code)} with no OUT. "
                          f"Counted as 0.00 h. {hint}", loc=s.location_code, on=s.date)
                self.line.step(f"{s.date} {store(s.location_code)}", f"IN {hhmm(s.start)}, no OUT: 0.00 h "
                               f"pending review", None, date=s.date, step="shift")
                continue
            if "missed_clock_in" in s.flags:
                self.flag("MISSED_CLOCK_IN", f"OUT at {hhmm(s.end)} at {store(s.location_code)} with no IN. "
                          f"Counted as 0.00 h", loc=s.location_code, on=s.date)
                continue
            if "odd_punches" in s.flags:
                raw = ", ".join(f"{p.timestamp:%H:%M} (row {p.source_row})" for p in s.raw_punches)
                self.flag("ODD_PUNCH_COUNT", f"{len(s.raw_punches)} punches at {store(s.location_code)} cannot be "
                          f"paired. Day counted as 0.00 h. Raw punches: {raw}", loc=s.location_code, on=s.date)
                self.line.step(f"{s.date} {store(s.location_code)}", f"{len(s.raw_punches)} punches ({raw}): "
                               f"0.00 h pending review", None, date=s.date, step="shift")
                continue
            if "overnight" in s.flags:
                self.flag("OVERNIGHT_SHIFT", f"Shift {s.start:%Y-%m-%d %H:%M} to {s.end:%Y-%m-%d %H:%M} "
                          f"({fmt_hours(s.hours)} h) crosses midnight", loc=s.location_code, on=s.date,
                          resolution=f"Counted as one shift on {s.date}")
            sched_locs = {x.location_code for x in sched}
            if not sched:
                self.flag("UNSCHEDULED_SHIFT", f"{fmt_hours(s.hours)} h worked at {store(s.location_code)} with "
                          f"no schedule. Paid at {store(s.location_code)} rate", loc=s.location_code, on=s.date)
            elif s.location_code not in sched_locs:
                s.pay_location_code = sched[0].location_code
                self.flag("WRONG_LOCATION", f"Scheduled at {store(s.pay_location_code)} but punches came from "
                          f"{store(s.location_code)}. Paid at {store(s.pay_location_code)} rate and cost "
                          f"allocated to {store(s.pay_location_code)}; confirm", loc=s.pay_location_code, on=s.date)
            note = f" (punched at {store(s.location_code)})" if s.pay_location_code else ""
            if "malformed_row_paid" in s.flags:
                note += " (paid from remaining punches; unreadable row pending review)"
            self.line.step(f"{s.date} {store(s.paid_at)}", f"{hhmm(s.start)}-{hhmm(s.end)} = "
                           f"{fmt_hours(s.hours)} h{note}", None, date=s.date, hours=s.hours,
                           step="shift")
            bump(self.line, s.paid_at, "hours", s.hours)

    def attendance(self) -> None:
        for sched in self.schedules:
            if not employed_on(self.e, sched.date) or not (self.period.start <= sched.date <= self.period.end):
                continue
            day = [s for s in self.shifts if s.date == sched.date]
            if not day:
                if sched.location_code in self.missing:
                    continue  # no data from that store; the blocking exception covers it
                self.absence(sched)
                continue
            if any(s.hours == 0 for s in day):
                continue  # unresolved punches are already flagged for review
            first_in = min(s.start for s in day)
            grace = timedelta(minutes=self.rules.late_grace_minutes)
            late_by = int((first_in - sched.start).total_seconds() // 60)
            if first_in > sched.start + grace:
                penalty = money(self.rules.late_penalty)
                self.line.penalties += penalty
                self.line.step("Late arrival penalty", f"{sched.date}: in {hhmm(first_in)}, scheduled "
                               f"{hhmm(sched.start)} + {self.rules.late_grace_minutes} min grace; late "
                               f"{late_by} min = {fmt_money(penalty)}", penalty, date=sched.date)
                self.flag("LATE_ARRIVAL", f"Clocked in {hhmm(first_in)}, scheduled {hhmm(sched.start)} "
                          f"({late_by} min late, grace {self.rules.late_grace_minutes} min)",
                          loc=sched.location_code, on=sched.date, resolution=f"Penalty {fmt_money(penalty)}")
            worked = sum((s.hours for s in day), ZERO)
            if not self.hourly and worked < sched.hours * self.rules.half_day_threshold:
                deduction = money(self.daily_rate / 2)
                self.deduct_absence(deduction, f"Half day {sched.date}", f"worked {fmt_hours(worked)} h < "
                                    f"{self.rules.half_day_threshold} x {fmt_hours(sched.hours)} h scheduled: "
                                    f"0.5 x {fmt_money(self.daily_rate)} = {fmt_money(deduction)}", sched.date)
                self.flag("HALF_DAY", f"Worked {fmt_hours(worked)} h of {fmt_hours(sched.hours)} h scheduled",
                          loc=sched.location_code, on=sched.date, resolution=f"Deducted {fmt_money(deduction)}")

    def deduct_absence(self, amount: Decimal, label: str, formula: str, on: date) -> None:
        self.line.absence_deductions += amount
        bump(self.line, self.e.home_location, "absence_deductions", amount)
        self.line.step(label, formula, amount, date=on)

    def absence(self, sched: ScheduledShift) -> None:
        leave = self.leaves.get(sched.date)
        loc = sched.location_code
        paid = None
        if leave is not None:
            paid = self.rules.leave_types.get(leave.leave_type)
            if paid is None:
                self.flag("UNKNOWN_LEAVE_TYPE", f"Leave type '{leave.leave_type}' is not in the rules; "
                          f"treated as unpaid until reviewed", loc=loc, on=sched.date)
                paid = False
            kind = f"{leave.leave_type} leave ({'paid' if paid else 'unpaid'} per rules)"
            if self.hourly and paid:
                rate = self.rate(loc) or ZERO
                pay = money(sched.hours * rate)
                self.line.regular_pay += pay
                bump(self.line, loc, "regular_pay", pay)
                self.line.step(f"Paid leave {sched.date}", f"{leave.leave_type}: {fmt_hours(sched.hours)} h "
                               f"scheduled x {fmt_money(rate)} = {fmt_money(pay)}", pay, date=sched.date)
                outcome = f"Paid {fmt_money(pay)} leave"
            elif not self.hourly and not paid:
                self.deduct_absence(self.daily_rate, f"Unpaid leave {sched.date}",
                                    f"1 day x {fmt_money(self.daily_rate)}", sched.date)
                outcome = f"Deducted {fmt_money(self.daily_rate)}"
            else:
                self.line.step(f"Leave {sched.date}", f"{kind}: no pay change", None, date=sched.date)
                outcome = "No pay change"
            if leave.informed:
                self.flag("ABSENCE_INFORMED", f"Absent, leave record: {kind}", loc=loc, on=sched.date,
                          resolution=outcome)
                return
        # Uninformed absence (no leave record, or a leave record entered without notice).
        parts = []
        if leave is None and not self.hourly:
            self.deduct_absence(self.daily_rate, f"Absence {sched.date}",
                                f"uninformed: 1 day x {fmt_money(self.daily_rate)}", sched.date)
            parts.append(f"deducted {fmt_money(self.daily_rate)}")
        penalty = money(self.rules.uninformed_penalty)
        if penalty:
            self.line.penalties += penalty
            self.line.step(f"Uninformed absence penalty {sched.date}", f"rules: {fmt_money(penalty)}", penalty,
                           date=sched.date)
            parts.append(f"penalty {fmt_money(penalty)}")
        detail = "no leave record" if leave is None else f"leave '{leave.leave_type}' recorded without notice"
        self.flag("ABSENCE_UNINFORMED", f"Scheduled {hhmm(sched.start)}-{hhmm(sched.end)} at {store(loc)}, no "
                  f"punches, {detail}: {', '.join(parts) or 'no pay change'}", loc=loc, on=sched.date)

    def weekly_hours(self) -> None:
        line, rules = self.line, self.rules
        exempt = self.e.pay_type in rules.ot_exempt_pay_types
        for week in self.weeks:
            for s in self.shifts:
                if s.hours and week.contains(s.date):
                    week.hours_by_loc[s.paid_at] = week.hours_by_loc.get(s.paid_at, ZERO) + s.hours
            if not week.hours_by_loc:
                continue
            for loc, hours in sorted(week.hours_by_loc.items()):
                rate = self.rate(loc)
                if rate is None:
                    self.flag("MISSING_RATE", f"No pay rate for {store(loc)}; {fmt_hours(hours)} h in week "
                              f"{week.index} unpaid until a rate is set", loc=loc)
                    rate = ZERO
                pay = money(hours * rate)
                week.straight_by_loc[loc] = pay
                line.regular_pay += pay
                bump(line, loc, "regular_pay", pay)
                line.step(f"Week {week.index} straight time {store(loc)}",
                          f"{fmt_hours(hours)} h x {fmt_money(rate)} = {fmt_money(pay)}", pay, week=week.index)
            parts = " + ".join(f"{store(loc)} {fmt_hours(h)}" for loc, h in sorted(week.hours_by_loc.items()))
            total = week.total_hours
            line.step(f"Week {week.index} hours", f"{parts} = {fmt_hours(total)}", None, week=week.index)
            if exempt or total <= rules.ot_threshold:
                continue
            week.ot_hours = total - rules.ot_threshold
            compute_premium(week, line, rules, label=f"Week {week.index} OT premium")
            if len(week.hours_by_loc) > 1:
                self.flag("MULTI_LOCATION_OVERTIME", f"Week {week.index}: {parts} = {fmt_hours(total)} h, "
                          f"{fmt_hours(week.ot_hours)} h over {fmt_hours(rules.ot_threshold)}",
                          on=week.start, resolution="Hours combined across stores")
            rates = {self.e.rates.get(loc) for loc in week.hours_by_loc}
            if len(rates) > 1:
                self.flag("BLENDED_RATE_OVERTIME", f"Week {week.index}: different store rates; OT premium on "
                          f"weighted rate {fmt_money(week.weighted_rate)} (unrounded in the premium) = {fmt_money(week.premium)}",
                          on=week.start, resolution="Weighted average regular rate")


def compute_premium(week: WeekHours, line: PayrollLine, rules: Rules, label: str,
                    extra_regular: Decimal = ZERO) -> Decimal:
    """Set ``week.premium`` from the weighted regular rate and return the change in premium."""
    total = week.total_hours
    earnings = week.straight_total + extra_regular
    # Full precision: only the premium (a money amount) is rounded. The trail shows the rate to cents.
    week.weighted_rate = earnings / total
    factor = rules.ot_multiplier - 1
    premium = money(factor * week.weighted_rate * week.ot_hours)
    extra = f" (incl. {fmt_money(extra_regular)} add-ons)" if extra_regular else ""
    line.step(f"Week {week.index} weighted rate", f"{fmt_money(earnings)}{extra} / {fmt_hours(total)} h = "
              f"{fmt_money(week.weighted_rate)} (unrounded in the premium)", money(week.weighted_rate),
              week=week.index, rate=week.weighted_rate)
    line.step(label, f"{factor} x ({fmt_money(earnings)} / {fmt_hours(total)} h) x {fmt_hours(week.ot_hours)} h "
              f"OT = {fmt_money(premium)}", premium, week=week.index)
    change = premium - week.premium
    for loc, amount in allocate(change, week.hours_by_loc).items():
        bump(line, loc, "overtime_pay", amount)
    line.overtime_pay += change
    week.premium = premium
    return change


UNRESOLVED = {"missed_clock_out", "missed_clock_in", "odd_punches"}


def settle_bad_rows(
    bad_rows: Iterable[PayrollException], shifts: list[Shift], period: Period,
) -> tuple[list[Shift], dict[str, dict[tuple[str, date], list[PayrollException]]], list[PayrollException]]:
    """Decide what an unparseable row for a known employee means for pay.

    A bad row dated inside the period puts that employee's day at that store up for
    review. If the remaining punches pair cleanly the day is paid from them; otherwise
    the day becomes a single 0 h shift with the raw punches attached. A row whose date
    can't be read also needs review. Only rows dated outside the period stay ``info``.
    """
    voided: dict[str, dict[tuple[str, date], list[PayrollException]]] = defaultdict(lambda: defaultdict(list))
    found: list[PayrollException] = []
    for bad in bad_rows:
        if bad.date is None:
            found.append(exc.make("MALFORMED_ROW", f"{bad.message}. Work date unreadable, so the affected day "
                                  f"is unknown: check this employee's punches", employee_id=bad.employee_id,
                                  location_code=bad.location_code, severity=exc.NEEDS_REVIEW))
        elif not period.start <= bad.date <= period.end:
            bad.message += ": outside this pay period, no effect on pay"
            bad.resolution = "Row skipped"
            found.append(bad)
        else:
            voided[bad.employee_id][(bad.location_code, bad.date)].append(bad)

    kept: list[Shift] = []
    removed: dict[tuple[str, str, date], list[Shift]] = defaultdict(list)
    for s in shifts:
        if (s.location_code, s.date) in voided.get(s.employee_id, {}):
            removed[(s.employee_id, s.location_code, s.date)].append(s)
        else:
            kept.append(s)
    for emp_id, days in voided.items():
        for (loc, day), bads in list(days.items()):
            day_shifts = removed[(emp_id, loc, day)]
            if day_shifts and all(s.hours > 0 and not set(s.flags) & UNRESOLVED for s in day_shifts):
                # The remaining punches pair cleanly: pay them, but a person confirms the lost punch.
                hours = sum((s.hours for s in day_shifts), ZERO)
                for s in day_shifts:
                    s.flags.append("malformed_row_paid")
                kept.extend(day_shifts)
                found.append(exc.make(
                    "MALFORMED_ROW",
                    f"{'; '.join(b.message for b in bads)}. Paid {fmt_hours(hours)} h from the remaining punches; "
                    f"confirm the unreadable punch was not a real break or extra shift.",
                    employee_id=emp_id, location_code=loc, on=day, severity=exc.NEEDS_REVIEW,
                ))
                del days[(loc, day)]
                continue
            raw = sorted((p for s in day_shifts for p in s.raw_punches), key=lambda p: (p.timestamp, p.source_row))
            start = raw[0].timestamp if raw else datetime.combine(day, time.min)
            kept.append(Shift(emp_id, loc, day, start, None, ZERO, ["malformed_row"], raw_punches=raw))
    return kept, {k: dict(v) for k, v in voided.items()}, found


def run_hr_stage(
    employees: Iterable[Employee],
    schedules: Iterable[ScheduledShift],
    punches: list[Punch],
    leaves: Iterable[LeaveRecord],
    period: Period,
    rules: Rules,
    missing_locations: set[str],
    bad_rows: Iterable[PayrollException] = (),
) -> tuple[dict[str, EmployeeHR], list[PayrollException]]:
    punches, found = drop_duplicates(punches, rules.duplicate_window_minutes)
    shifts = [s for s in build_shifts(punches, rules) if period.start <= s.date <= period.end]
    shifts, voided, settled = settle_bad_rows(bad_rows, shifts, period)
    found.extend(settled)

    by_emp_sched: dict[str, list[ScheduledShift]] = defaultdict(list)
    for s in schedules:
        by_emp_sched[s.employee_id].append(s)
    by_emp_shift: dict[str, list[Shift]] = defaultdict(list)
    for s in shifts:
        by_emp_shift[s.employee_id].append(s)
    by_emp_leave: dict[str, dict[date, LeaveRecord]] = defaultdict(dict)
    for lv in leaves:
        by_emp_leave[lv.employee_id][lv.date] = lv

    results: dict[str, EmployeeHR] = {}
    for e in employees:
        if e.hire_date > period.end or (e.exit_date and e.exit_date < period.start):
            continue
        run = _EmployeeRun(e, period, rules, by_emp_sched[e.id], by_emp_shift[e.id], by_emp_leave[e.id],
                           missing_locations, voided.get(e.id))
        results[e.id] = run.run()
        found.extend(run.found)
    return results, found
