"""Write the synthetic demo inputs: three store exports in different formats plus HR/finance files.

Every scenario is planted on purpose at a known employee and day of the period, and recorded in
``scenario_manifest.json`` (with the actual dates) so tests can find it. A fixed seed keeps the
output identical between runs. All names are made up.

Every date is relative to the period start (day 0). The start must fall on the rules' week start
day, so every planted scenario lands on the same weekday whatever period is generated.

Usage: python -m core.demo_data.generator [--out DIR] [--period YYYY-MM-DD] [--missing-store CODE]
"""
from __future__ import annotations

import argparse
import calendar
import csv
import json
import random
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Optional

from core.engine.config import DEFAULT_RULES_PATH, Rules, load_rules

SEED = 20260907
DEFAULT_PERIOD_START = date(2026, 9, 7)  # Monday
DEFAULT_OUTPUT = Path(__file__).resolve().parent / "output"

LOCATIONS = [
    {"code": "A", "name": "Northgate Market", "timezone": "America/Chicago", "export_format": "store_a"},
    {"code": "B", "name": "Riverside Plaza", "timezone": "America/Chicago", "export_format": "store_b"},
    {"code": "C", "name": "Lakeview Commons", "timezone": "America/Chicago", "export_format": "store_c"},
]
TIME_FILES = {"A": "store_a_timeclock.csv", "B": "store_b_timeclock.csv", "C": "store_c_timeclock.csv"}


class PeriodStartError(ValueError):
    """The requested period start is not on the rules' week start day."""


def _emp(id, name, home, rates=None, salary=None, ext=None, hired=-966, exits=None):
    """Core employee. ``hired`` and ``exits`` are day offsets from the period start."""
    return {
        "id": id, "name": name, "home_location": home,
        "pay_type": "salaried" if salary else "hourly",
        "rates": rates or {}, "salary_per_period": salary,
        "hired": hired, "exits": exits, "external_ids": ext,
    }


# The 18 core employees, each cast for a scenario. Offsets: day 0 is the period's first Monday.
CORE_EMPLOYEES = [
    _emp("E01", "Avery Holt", "A", salary="2000.00", ext={"A": "1001"}, hired=-1286),
    _emp("E02", "Blake Moreno", "A", {"A": "16.00"}, ext={"A": "1002"}),
    _emp("E03", "Casey Lin", "A", {"A": "15.50"}, ext={"A": "1003"}),
    _emp("E04", "Devon Price", "A", {"A": "17.00"}, ext={"A": "1004"}),
    _emp("E05", "Emery Walsh", "A", {"A": "16.50"}, ext={"A": "1005"}),
    _emp("E06", "Finley Cruz", "A", {"A": "16.00", "B": "16.00"}, ext={"A": "1006", "B": "RV-206"}),
    _emp("E07", "Harper Lane", "B", salary="2200.00", ext={"B": "RV-201"}, hired=-1559),
    _emp("E08", "Jordan Ellis", "B", {"B": "15.00"}, ext={"B": "RV-202"}),
    _emp("E09", "Kai Bennett", "B", {"B": "15.50"}, ext={"B": "RV-203"}),
    _emp("E10", "Logan Reyes", "B", {"B": "16.00", "A": "17.00"}, ext={"B": "RV-204", "A": "1010"}),
    _emp("E11", "Morgan Shah", "A", {"A": "16.00", "B": "18.00"}, ext={"A": "1011", "B": "RV-211"}),
    _emp("E12", "Nico Fischer", "B", {"B": "15.00"}, ext={"B": "RV-205"}),
    _emp("E13", "Parker Dunn", "C", salary="2000.00", ext={"C": "Parker Dunn"}, hired=-2044, exits=4),  # Fri wk 1
    _emp("E14", "Quinn Harlow", "C", salary="2000.00", ext={"C": "Quinn Harlow"}, hired=2),  # Wed wk 1
    _emp("E15", "Riley Okafor", "C", {"C": "15.00"}, ext={"C": "Riley Okafor"}),
    _emp("E16", "Sage Delgado", "C", {"C": "16.00"}, ext={"C": "Sage Delgado"}),
    _emp("E17", "Taylor Voss", "C", {"C": "15.50"}, ext={"C": "Taylor Voss"}),
    _emp("E18", "Rowan Pike", "C", {"C": "15.00", "A": "16.00"}, ext={"C": "Rowan Pike", "A": "1018"}),
]

# Employees whose punches drift a few minutes from the schedule (background noise).
JITTER = {"E09", "E17", "E18"}

# HR and finance inputs. ``day`` / ``start_period`` are offsets from the period start.
LEAVES = [
    {"employee_id": "E09", "day": 7, "leave_type": "sick", "paid": True, "informed": True},  # Mon wk 2
    {"employee_id": "E17", "day": 9, "leave_type": "unpaid", "paid": False, "informed": True},  # Wed wk 2
]
ADDONS = [
    {"employee_id": "E15", "day": 3, "kind": "commission", "amount": "90.00", "include_in_regular_rate": True},
    {"employee_id": "E16", "day": 11, "kind": "bonus", "amount": "100.00", "include_in_regular_rate": False},
]
LOANS = [
    {"employee_id": "E12", "kind": "advance", "total": "600.00", "installment": "150.00",
     "start_period": -14, "paid_to_date": "150.00"},  # started the previous period
    {"employee_id": "E16", "kind": "loan", "total": "2000.00", "installment": "400.00",
     "start_period": 0, "paid_to_date": "0.00"},
    {"employee_id": "E13", "kind": "loan", "total": "1000.00", "installment": "100.00",
     "start_period": -42, "paid_to_date": "200.00"},
]


def t(hhmm: str) -> time:
    return time.fromisoformat(hhmm)


def check_period_start(start: date, rules: Rules) -> None:
    """Refuse a start that is not on the week start day; never shift it silently."""
    week_start = rules.week_start
    if start.weekday() == week_start:
        return
    before = start - timedelta(days=(start.weekday() - week_start) % 7)
    after = before + timedelta(days=7)
    raise PeriodStartError(
        f"{start.isoformat()} is a {calendar.day_name[start.weekday()]}. Pay periods in the demo rules start on "
        f"{calendar.day_name[week_start]}. Try {before.isoformat()} or {after.isoformat()}.")


class Plan:
    """Everything dated, for one period. ``day(n)`` is the period start plus n days."""

    def __init__(self, start: date, length_days: int):
        self.start = start
        self.length_days = length_days

    def day(self, n: int) -> date:
        return self.start + timedelta(days=n)

    def at(self, n: int, hhmm: str) -> datetime:
        return datetime.combine(self.day(n), t(hhmm))

    @property
    def end(self) -> date:
        return self.day(self.length_days - 1)

    def employees(self) -> list[dict]:
        rows = []
        for e in CORE_EMPLOYEES:
            row = {k: v for k, v in e.items() if k not in ("hired", "exits")}
            row["hire_date"] = self.day(e["hired"]).isoformat()
            row["exit_date"] = self.day(e["exits"]).isoformat() if e["exits"] is not None else None
            rows.append({k: row[k] for k in ("id", "name", "home_location", "pay_type", "rates",
                                              "salary_per_period", "hire_date", "exit_date", "external_ids")})
        return rows

    def leaves(self) -> list[dict]:
        return [{**{k: v for k, v in r.items() if k != "day"}, "date": self.day(r["day"]).isoformat()}
                for r in LEAVES]

    def addons(self) -> list[dict]:
        return [{**{k: v for k, v in r.items() if k != "day"}, "date": self.day(r["day"]).isoformat()}
                for r in ADDONS]

    def loans(self) -> list[dict]:
        return [{**r, "start_period": self.day(r["start_period"]).isoformat()} for r in LOANS]


def build_schedules(plan: Plan, employees: list[dict]) -> list[dict]:
    days = [plan.day(i) for i in range(plan.length_days)]
    weekdays = [x for x in days if x.weekday() < 5]
    d = plan.day
    rows: list[dict] = []

    def add(emp, loc, day, s, e):
        rows.append({"employee_id": emp, "location_code": loc, "date": day, "start": s, "end": e})

    for emp in employees:
        eid, home = emp["id"], emp["home_location"]
        hire = date.fromisoformat(emp["hire_date"])
        exit_ = date.fromisoformat(emp["exit_date"]) if emp["exit_date"] else None
        for day in weekdays:
            if day < hire or (exit_ and day > exit_):
                continue
            if emp["pay_type"] == "salaried":
                add(eid, home, day, "08:00", "17:00")
            elif eid == "E04" and day == d(4):
                add(eid, home, day, "22:00", "06:00")  # scenario 6: overnight (Fri wk 1)
            elif eid == "E06" and day == d(4):
                add(eid, "B", day, "10:00", "16:00")  # scenario 10
            elif eid == "E11" and day == d(11):
                add(eid, "B", day, "10:00", "16:00")  # scenario 12
            elif eid == "E15" and day < d(7):
                add(eid, home, day, "09:00", "18:00")  # scenario 14: 45 h week
            elif eid == "E18" and day in (d(3), d(4)):
                add(eid, "A", day, "09:00", "17:00")  # works two stores, no overtime
            else:
                add(eid, home, day, "09:00", "17:00")
    # Saturday shifts at the second store push these two over 40 hours.
    add("E06", "B", d(5), "10:00", "16:00")
    add("E11", "B", d(12), "10:00", "16:00")
    # Boundary: overnight shift starting on the period's last day (Sunday), ending the morning after.
    # E09 has sick leave that Monday, so the week stays under 40 worked hours.
    add("E09", "B", d(plan.length_days - 1), "22:00", "06:00")
    rows.sort(key=lambda r: (r["date"], r["employee_id"]))
    return rows


def build_shifts(schedules: list[dict], rng: random.Random) -> list[dict]:
    """Actual worked shifts (location, in, out) before the scenario overrides."""
    shifts = []
    for s in schedules:
        start = datetime.combine(s["date"], t(s["start"]))
        end = datetime.combine(s["date"], t(s["end"]))
        if end <= start:
            end += timedelta(days=1)
        if s["employee_id"] in JITTER:
            # A few minutes late in (inside the grace period) and early out: never adds overtime.
            start += timedelta(minutes=rng.randint(0, 4))
            end -= timedelta(minutes=rng.randint(0, 4))
        shifts.append({"employee_id": s["employee_id"], "location": s["location_code"], "date": s["date"],
                       "in": start, "out": end})
    return shifts


def apply_scenarios(plan: Plan, shifts: list[dict]) -> tuple[list[dict], dict]:
    """Plant the scenarios. Returns the edited shifts and the manifest entries (with actual dates)."""
    d, at = plan.day, plan.at

    def find(emp, day):
        return next(s for s in shifts if s["employee_id"] == emp and s["date"] == day)

    def remove(emp, day):
        shifts.remove(find(emp, day))

    scenarios: dict[str, list[dict]] = {}

    def record(n, emp=None, day=None, location=None, **extra):
        scenarios.setdefault(str(n), []).append(
            {"employee": emp, "date": day.isoformat() if day else None, "location": location, **extra})

    # 1: malformed rows in store A, unexpected column in store C (all added when writing files)
    record(1, "E02", d(2), "A", code="MALFORMED_ROW", severity="needs_review", paid_hours="8.00",
           note="row with time '9:6O' inside the period: remaining punches pair, paid and flagged for review")
    record(1, None, None, "C", code="UNKNOWN_COLUMN", column="Dept",
           note="extra 'Dept' column: a file-level note on the Inputs screen, not an exception")
    # 3: duplicate IN at store A
    find("E02", d(1))["duplicate_in"] = True
    record(3, "E02", d(1), "A", code="DUPLICATE_PUNCH")
    # 4: missed clock-out at store A
    find("E03", d(3))["out"] = None
    record(4, "E03", d(3), "A", code="MISSED_CLOCK_OUT", scheduled_hours="8.00")
    # 5: odd number of punches at store B (back from lunch, never clocked out)
    find("E08", d(2))["extra_punches"] = [at(2, "09:00"), at(2, "13:00"), at(2, "13:30")]
    record(5, "E08", d(2), "B", code="ODD_PUNCH_COUNT", punches=3)
    # 6: overnight shift (already scheduled 22:00-06:00)
    record(6, "E04", d(4), "A", code="OVERNIGHT_SHIFT", hours="8.00")
    # 7: late beyond grace, and late within grace
    find("E02", d(7))["in"] = at(7, "09:30")
    record(7, "E02", d(7), "A", code="LATE_ARRIVAL", minutes_late=30, penalty="5.00")
    find("E05", d(8))["in"] = at(8, "09:06")
    record(7, "E05", d(8), "A", code=None, minutes_late=6, penalty="0.00", note="within grace")
    # 8: salaried half day
    find("E01", d(2))["out"] = at(2, "11:30")
    record(8, "E01", d(2), "A", code="HALF_DAY", deduction="100.00")
    # 9: absences: uninformed (salaried), informed paid (hourly), informed unpaid (hourly)
    remove("E07", d(10))
    record(9, "E07", d(10), "B", code="ABSENCE_UNINFORMED", severity="needs_review", deduction="220.00",
           penalty="25.00")
    remove("E09", d(7))
    record(9, "E09", d(7), "B", code="ABSENCE_INFORMED", severity="info", leave_type="sick", leave_pay="124.00")
    remove("E17", d(9))
    record(9, "E17", d(9), "C", code="ABSENCE_INFORMED", severity="info", leave_type="unpaid", leave_pay="0.00")
    # 10: two stores, one week (same rate)
    record(10, "E06", d(0), None, code="MULTI_LOCATION_OVERTIME", week=1, hours={"A": "32.00", "B": "12.00"},
           ot_hours="4.00", premium="32.00")
    # 11: worked at store A while scheduled at store B
    find("E10", d(9))["location"] = "A"
    record(11, "E10", d(9), "B", code="WRONG_LOCATION", punched_at="A", pay="128.00")
    # 12: different rate per store, with overtime
    record(12, "E11", d(7), None, code="BLENDED_RATE_OVERTIME", week=2, weighted_rate="16.55",  # display only
           ot_hours="4.00", premium="33.09")
    # 13: advance within cap, loan over cap
    record(13, "E12", None, None, code="LOAN_DEDUCTED", severity="info", deducted="150.00", balance_after="300.00")
    record(13, "E16", None, None, code="LOAN_CAPPED", severity="needs_review", deducted="276.00", carried="124.00")
    # 14: commission included in regular rate during an overtime week
    record(14, "E15", d(3), None, code="ADDON_OT_RECALC", week=1, premium_before="37.50", premium_after="42.50")
    # 15: new hire mid-period (salaried)
    record(15, "E14", d(2), "C", code="NEW_HIRE_PRORATED", days=8, salary="1600.00")
    # 16: exit mid-period with an outstanding loan
    record(16, "E13", d(4), "C", code="EXIT_FINAL_SETTLEMENT", salary="1000.00", loan_deducted="200.00",
           unsettled="600.00")
    # 17: review gate (services.finalize)
    record(17, note="finalize refuses while needs_review exceptions are open or blocking ones exist")
    # 18: custom rules from config
    record(18, "E16", None, None, code="CUSTOM_RULE_APPLIED", retirement="41.40", table="13.80")
    # 19: pay period boundaries (a shift belongs to the day it started)
    last = plan.length_days - 1
    edge = find("E09", d(last))
    edge["in"], edge["out"] = at(last, "22:00"), at(last + 1, "06:00")  # exact, no jitter
    record(19, "E09", d(last), "B", code="OVERNIGHT_SHIFT", hours="8.00", pay="124.00",
           note="starts on the last day, ends the morning after: fully counted in this payroll")
    shifts.append({"employee_id": "E12", "location": "B", "date": d(-1), "in": at(-1, "22:00"),
                   "out": at(0, "06:00"), "edge": True})
    record(19, "E12", d(-1), "B", code=None, hours="0.00",
           note="starts the evening before the period: belongs to the previous payroll, not counted or shown")
    return shifts, scenarios


def edge_shifts(plan: Plan, employees: list[dict]) -> list[dict]:
    """Normal shifts on the day before the period and the day after it: real exports spill past the edges.

    They belong to other pay periods, so the engine skips them.
    """
    shifts = []

    def add(emp, loc, n, start, end):
        shifts.append({"employee_id": emp, "location": loc, "date": plan.day(n), "in": plan.at(n, start),
                       "out": plan.at(n, end), "edge": True})

    # The Sunday before: a few weekend shifts.
    add("E04", "A", -1, "10:00", "16:00")
    add("E08", "B", -1, "10:00", "16:00")
    add("E16", "C", -1, "10:00", "16:00")
    # The Monday after: everyone still employed works their usual shift.
    after = plan.length_days
    for e in employees:
        if e["exit_date"] and date.fromisoformat(e["exit_date"]) < plan.day(after):
            continue
        if e["pay_type"] == "salaried":
            add(e["id"], e["home_location"], after, "08:00", "17:00")
        else:
            add(e["id"], e["home_location"], after, "09:00", "17:00")
    return shifts


def _write_csv(path: Path, header: list[str], rows: list[list]) -> None:
    with open(path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(header)
        writer.writerows(rows)


def _ext(emp_id: str, loc: str) -> str:
    return next(e for e in CORE_EMPLOYEES if e["id"] == emp_id)["external_ids"][loc]


def write_store_a(path: Path, shifts: list[dict], plan: Plan) -> None:
    rows = []
    for s in shifts:
        if s["location"] != "A":
            continue
        emp = _ext(s["employee_id"], "A")
        rows.append([emp, s["in"].date().isoformat(), s["in"].strftime("%H:%M"), "IN"])
        if s.get("duplicate_in"):
            rows.append([emp, s["in"].date().isoformat(), (s["in"] + timedelta(minutes=1)).strftime("%H:%M"), "IN"])
        if s["out"]:
            rows.append([emp, s["out"].date().isoformat(), s["out"].strftime("%H:%M"), "OUT"])
    rows.append([_ext("E02", "A"), plan.day(2).isoformat(), "9:6O", "IN"])  # scenario 1: malformed, affects pay
    rows.sort(key=lambda r: (r[1], r[0], r[2]))
    _write_csv(path, ["EmpID", "Date", "Time", "Type"], rows)


def write_store_b(path: Path, shifts: list[dict], plan: Plan) -> None:
    rows = []
    for s in shifts:
        if s["location"] != "B":
            continue
        times = s.get("extra_punches") or [x for x in (s["in"], s["out"]) if x]
        rows.extend([_ext(s["employee_id"], "B"), x] for x in times)
    rows.sort(key=lambda r: (r[1], r[0]))
    _write_csv(path, ["employee_code", "punch_datetime"], [[c, x.strftime("%m/%d/%Y %I:%M %p")] for c, x in rows])


def write_store_c(path: Path, shifts: list[dict], plan: Plan) -> None:
    rows = []
    for s in shifts:
        if s["location"] != "C":
            continue
        out = s["out"].strftime("%H:%M") if s["out"] else ""
        rows.append([_ext(s["employee_id"], "C"), s["date"].isoformat(), s["in"].strftime("%H:%M"), out, "Floor"])
    rows.sort(key=lambda r: (r[1], r[0]))
    _write_csv(path, ["Name", "Date", "In", "Out", "Dept"], rows)  # 'Dept' is scenario 1's unknown column


def generate(output_dir: Path | str = DEFAULT_OUTPUT, missing_store: Optional[str] = None,
             period_start: date = DEFAULT_PERIOD_START, rules: Optional[Rules] = None) -> dict:
    """Write the demo inputs for the period starting ``period_start``. Raises ``PeriodStartError`` (and
    writes nothing) when the start is not on the rules' week start day."""
    rules = rules or load_rules(DEFAULT_RULES_PATH)
    check_period_start(period_start, rules)
    plan = Plan(period_start, rules.period_length_days)

    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    for old in TIME_FILES.values():
        (out / old).unlink(missing_ok=True)
    rng = random.Random(SEED)

    employees = plan.employees()
    schedules = build_schedules(plan, employees)
    shifts, scenarios = apply_scenarios(plan, build_shifts(schedules, rng))
    shifts += edge_shifts(plan, employees)
    writers = {"A": write_store_a, "B": write_store_b, "C": write_store_c}
    for code, writer in writers.items():
        if code != (missing_store or "").upper():
            writer(out / TIME_FILES[code], shifts, plan)
    if missing_store:
        scenarios["2"] = [{"employee": None, "date": None, "location": missing_store.upper(),
                           "code": "STORE_FILE_MISSING", "severity": "blocking"}]
    else:
        scenarios["2"] = [{"employee": None, "date": None, "location": None, "code": "STORE_FILE_MISSING",
                           "note": "generate with --missing-store CODE to plant"}]

    leaves, addons, loans = plan.leaves(), plan.addons(), plan.loans()
    (out / "locations.json").write_text(json.dumps(LOCATIONS, indent=2))
    (out / "employees.json").write_text(json.dumps(employees, indent=2))
    _write_csv(out / "schedules.csv", ["employee_id", "location_code", "date", "start", "end"],
               [[r["employee_id"], r["location_code"], r["date"].isoformat(), r["start"], r["end"]]
                for r in schedules])
    _write_csv(out / "leaves.csv", ["employee_id", "date", "leave_type", "paid", "informed"],
               [[r["employee_id"], r["date"], r["leave_type"], r["paid"], r["informed"]] for r in leaves])
    _write_csv(out / "addons.csv", ["employee_id", "date", "kind", "amount", "include_in_regular_rate"],
               [[r["employee_id"], r["date"], r["kind"], r["amount"], r["include_in_regular_rate"]] for r in addons])
    _write_csv(out / "loans_advances.csv",
               ["employee_id", "kind", "total", "installment", "start_period", "paid_to_date"],
               [[r[k] for k in ("employee_id", "kind", "total", "installment", "start_period", "paid_to_date")]
                for r in loans])

    manifest = {
        "seed": SEED,
        "period_start": period_start.isoformat(),
        "period_end": plan.end.isoformat(),
        "missing_store": missing_store.upper() if missing_store else None,
        "time_files": TIME_FILES,
        "scenarios": dict(sorted(scenarios.items(), key=lambda kv: int(kv[0]))),
    }
    (out / "scenario_manifest.json").write_text(json.dumps(manifest, indent=2))
    return manifest


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--period", type=date.fromisoformat, default=DEFAULT_PERIOD_START,
                        help="Period start date, YYYY-MM-DD (must be on the rules' week start day)")
    parser.add_argument("--missing-store", default=None, help="Leave this store's export out (scenario 2)")
    args = parser.parse_args(argv)
    try:
        manifest = generate(args.out, args.missing_store, args.period)
    except PeriodStartError as e:
        parser.exit(2, f"{e}\n")
    print(f"Wrote demo inputs for {manifest['period_start']} to {args.out}")


if __name__ == "__main__":
    main()
