"""Store A: ``EmpID,Date,Time,Type`` with explicit IN/OUT rows and ISO dates."""
from __future__ import annotations

from datetime import date, datetime

from ..types import Punch
from .base import Importer, RowError, check_values
from .diagnose import diagnose_date_iso, diagnose_time24, parse_date_iso, parse_time24


class StoreAImporter(Importer):
    format_name = "store_a"
    required_columns = ("EmpID", "Date", "Time", "Type")
    department_column = "Dept"

    def row_identifier(self, row):
        return row["EmpID"]

    def row_date(self, row):
        return date.fromisoformat(row["Date"])

    def parse_row(self, row, employee_id, source_row):
        kind = row["Type"].lower()
        if kind not in ("in", "out"):
            raise RowError(f"unknown punch type '{row['Type']}'")
        check_values([("Date", row["Date"], parse_date_iso, diagnose_date_iso),
                      ("Time", row["Time"], parse_time24, diagnose_time24)])
        at = datetime.combine(parse_date_iso(row["Date"]), parse_time24(row["Time"]))
        return [Punch(employee_id, self.location_code, at, kind, source_row)]
