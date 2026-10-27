"""Store B: ``employee_code,punch_datetime``. No IN/OUT; punches alternate. ``MM/DD/YYYY hh:mm AM/PM``."""
from __future__ import annotations

from datetime import datetime

from ..types import Punch
from .base import Importer, check_values
from .diagnose import diagnose_datetime_12h, parse_datetime_12h

class StoreBImporter(Importer):
    format_name = "store_b"
    required_columns = ("employee_code", "punch_datetime")
    department_column = "department"

    def row_identifier(self, row):
        return row["employee_code"]

    def row_date(self, row):
        return datetime.strptime(row["punch_datetime"].split()[0], "%m/%d/%Y").date()

    def parse_row(self, row, employee_id, source_row):
        check_values([("punch_datetime", row["punch_datetime"], parse_datetime_12h, diagnose_datetime_12h)])
        at = parse_datetime_12h(row["punch_datetime"])
        return [Punch(employee_id, self.location_code, at, None, source_row)]
