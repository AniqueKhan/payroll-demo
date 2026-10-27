"""Store C: ``Name,Date,In,Out``. One row per shift, employee identified by name.

An empty ``Out`` is a missed clock-out. ``Out`` earlier than ``In`` means the shift ended the next day.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from ..types import Punch
from .base import Importer, check_values
from .diagnose import diagnose_date_iso, diagnose_time24, parse_date_iso, parse_time24


class StoreCImporter(Importer):
    format_name = "store_c"
    required_columns = ("Name", "Date", "In", "Out")
    department_column = "Dept"

    def row_identifier(self, row):
        return row["Name"]

    def row_date(self, row):
        return date.fromisoformat(row["Date"])

    def parse_row(self, row, employee_id, source_row):
        checks = [("Date", row["Date"], parse_date_iso, diagnose_date_iso),
                  ("In", row["In"], parse_time24, diagnose_time24)]
        if row["Out"]:
            checks.append(("Out", row["Out"], parse_time24, diagnose_time24))
        check_values(checks)
        day = parse_date_iso(row["Date"])
        start = datetime.combine(day, parse_time24(row["In"]))
        end = None
        if row["Out"]:
            end = datetime.combine(day, parse_time24(row["Out"]))
            if end <= start:
                end += timedelta(days=1)
        punches = [Punch(employee_id, self.location_code, start, "in", source_row)]
        if end is not None:
            punches.append(Punch(employee_id, self.location_code, end, "out", source_row))
        return punches
