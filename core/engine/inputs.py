"""Read the master data and input files (as written by the demo generator) into engine types."""
from __future__ import annotations

import csv
import json
from datetime import date, datetime, time, timedelta
from pathlib import Path

from .config import Rules
from .explain import to_decimal
from .run import PayrollInputs, make_period
from .types import Employee, LeaveRecord, LoanAdvance, Location, PayAddOn, ScheduledShift

MANIFEST = "scenario_manifest.json"


def _bool(value: str) -> bool:
    return str(value).strip().lower() in ("1", "true", "yes", "y")


def _rows(path: Path) -> list[dict[str, str]]:
    with open(path, encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def read_locations(path: Path) -> list[Location]:
    return [Location(**row) for row in json.loads(Path(path).read_text())]


def employee_from_dict(row: dict) -> Employee:
    return Employee(
        id=row["id"],
        name=row["name"],
        home_location=row["home_location"],
        pay_type=row["pay_type"],
        rates={k: to_decimal(v) for k, v in (row.get("rates") or {}).items()},
        salary_per_period=None if row.get("salary_per_period") in (None, "") else to_decimal(row["salary_per_period"]),
        hire_date=date.fromisoformat(row["hire_date"]),
        exit_date=date.fromisoformat(row["exit_date"]) if row.get("exit_date") else None,
        external_ids=dict(row.get("external_ids") or {}),
    )


def read_employees(path: Path) -> list[Employee]:
    return [employee_from_dict(row) for row in json.loads(Path(path).read_text())]


def schedule(employee_id: str, location_code: str, day: date, start: time, end: time) -> ScheduledShift:
    start_dt = datetime.combine(day, start)
    end_dt = datetime.combine(day, end)
    if end_dt <= start_dt:
        end_dt += timedelta(days=1)
    return ScheduledShift(employee_id, location_code, day, start_dt, end_dt)


def read_schedules(path: Path) -> list[ScheduledShift]:
    return [
        schedule(r["employee_id"], r["location_code"], date.fromisoformat(r["date"]),
                 time.fromisoformat(r["start"]), time.fromisoformat(r["end"]))
        for r in _rows(path)
    ]


def read_leaves(path: Path) -> list[LeaveRecord]:
    return [
        LeaveRecord(r["employee_id"], date.fromisoformat(r["date"]), r["leave_type"], _bool(r["paid"]),
                    _bool(r["informed"]))
        for r in _rows(path)
    ]


def read_addons(path: Path) -> list[PayAddOn]:
    return [
        PayAddOn(r["employee_id"], date.fromisoformat(r["date"]), r["kind"], to_decimal(r["amount"]),
                 _bool(r["include_in_regular_rate"]))
        for r in _rows(path)
    ]


def read_loans(path: Path) -> list[LoanAdvance]:
    return [
        LoanAdvance(r["employee_id"], r["kind"], to_decimal(r["total"]), to_decimal(r["installment"]),
                    date.fromisoformat(r["start_period"]), to_decimal(r["paid_to_date"]))
        for r in _rows(path)
    ]


def read_manifest(directory: Path) -> dict:
    return json.loads((Path(directory) / MANIFEST).read_text())


def load_inputs_from_dir(directory: str | Path, rules: Rules) -> PayrollInputs:
    directory = Path(directory)
    manifest = read_manifest(directory)
    period = make_period(date.fromisoformat(manifest["period_start"]), rules)
    time_files = {
        code: directory / name
        for code, name in manifest["time_files"].items()
        if (directory / name).exists()
    }
    return PayrollInputs(
        period=period,
        locations=read_locations(directory / "locations.json"),
        employees=read_employees(directory / "employees.json"),
        schedules=read_schedules(directory / "schedules.csv"),
        time_files=time_files,
        leaves=read_leaves(directory / "leaves.csv"),
        addons=read_addons(directory / "addons.csv"),
        loans=read_loans(directory / "loans_advances.csv"),
    )
