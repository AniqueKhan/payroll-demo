"""Store B: ``employee_code,punch_datetime``. No IN/OUT; punches alternate. ``MM/DD/YYYY hh:mm AM/PM``."""
from __future__ import annotations

from datetime import datetime

from ..types import Punch
from .base import Importer, RowError

DATETIME_FORMAT = "%m/%d/%Y %I:%M %p"


class StoreBImporter(Importer):
    format_name = "store_b"
    required_columns = ("employee_code", "punch_datetime")

    def row_identifier(self, row):
        return row["employee_code"]

    def row_date(self, row):
        return datetime.strptime(row["punch_datetime"].split()[0], "%m/%d/%Y").date()

    def parse_row(self, row, employee_id, source_row):
        try:
            at = datetime.strptime(row["punch_datetime"], DATETIME_FORMAT)
        except ValueError:
            raise RowError(f"bad punch_datetime '{row['punch_datetime']}'")
        return [Punch(employee_id, self.location_code, at, None, source_row)]
