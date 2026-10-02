"""Store A: ``EmpID,Date,Time,Type`` with explicit IN/OUT rows and ISO dates."""
from __future__ import annotations

from datetime import date, datetime, time

from ..types import Punch
from .base import Importer, RowError


class StoreAImporter(Importer):
    format_name = "store_a"
    required_columns = ("EmpID", "Date", "Time", "Type")

    def row_identifier(self, row):
        return row["EmpID"]

    def row_date(self, row):
        return date.fromisoformat(row["Date"])

    def parse_row(self, row, employee_id, source_row):
        kind = row["Type"].lower()
        if kind not in ("in", "out"):
            raise RowError(f"unknown punch type '{row['Type']}'")
        try:
            day = date.fromisoformat(row["Date"])
            at = time.fromisoformat(row["Time"])
        except ValueError:
            raise RowError(f"bad date/time '{row['Date']} {row['Time']}'")
        return [Punch(employee_id, self.location_code, datetime.combine(day, at), kind, source_row)]
