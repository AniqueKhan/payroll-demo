"""Store C: ``Name,Date,In,Out``. One row per shift, employee identified by name.

An empty ``Out`` is a missed clock-out. ``Out`` earlier than ``In`` means the shift ended the next day.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta

from ..types import Punch
from .base import Importer, RowError


class StoreCImporter(Importer):
    format_name = "store_c"
    required_columns = ("Name", "Date", "In", "Out")

    def row_identifier(self, row):
        return row["Name"]

    def parse_row(self, row, employee_id, source_row):
        try:
            day = date.fromisoformat(row["Date"])
            start = datetime.combine(day, time.fromisoformat(row["In"]))
            end = None
            if row["Out"]:
                end = datetime.combine(day, time.fromisoformat(row["Out"]))
                if end <= start:
                    end += timedelta(days=1)
        except ValueError:
            raise RowError(f"bad date/time '{row['Date']} {row['In']}-{row['Out']}'")
        punches = [Punch(employee_id, self.location_code, start, "in", source_row)]
        if end is not None:
            punches.append(Punch(employee_id, self.location_code, end, "out", source_row))
        return punches
