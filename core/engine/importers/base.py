"""Importer interface: turn one store's time clock export into normalized ``Punch`` objects.

Importers never raise on bad data. Unreadable files, unexpected columns, bad rows and
unknown identifiers all come back as exceptions next to the punches that did parse.
"""
from __future__ import annotations

import csv
import io
from abc import ABC, abstractmethod
from dataclasses import replace
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from typing import IO, Iterable, Optional, Union

from .. import exceptions as exc
from ..types import Employee, PayrollException, Period, Punch

FileLike = Union[str, Path, IO[str]]


class RowError(ValueError):
    """A row that cannot be turned into punches, with an explanation of each value that couldn't be read."""

    def __init__(self, message: str, diagnoses=()):
        super().__init__(message)
        self.diagnoses = list(diagnoses)

    @classmethod
    def from_diagnoses(cls, diagnoses) -> "RowError":
        return cls(" ".join(d.summary() for d in diagnoses), diagnoses)


def check_values(checks) -> None:
    """``checks`` is a list of (column, value, parser, diagnoser). Raise one RowError explaining every value
    the parser rejects."""
    bad = []
    for column, value, parser, diagnoser in checks:
        try:
            parser(value)
        except ValueError:
            bad.append(diagnoser(column, value))
    if bad:
        raise RowError.from_diagnoses(bad)


def sentence(text: str) -> str:
    """End the text with a full stop unless it already ends a sentence."""
    return text if text.endswith((".", "?", "!")) else f"{text}."


def normalize_id(value: str) -> str:
    return " ".join(value.split()).casefold()


class Importer(ABC):
    format_name: str = ""
    required_columns: tuple[str, ...] = ()
    department_column: Optional[str] = None  # optional column; the department is read from the clock-in row

    @property
    def supported_columns(self) -> tuple[str, ...]:
        return self.required_columns + ((self.department_column,) if self.department_column else ())

    def __init__(self, location_code: str, employees: Iterable[Employee]):
        self.location_code = location_code
        self.id_map = {
            normalize_id(e.external_ids[location_code]): e.id
            for e in employees
            if location_code in e.external_ids
        }

    @abstractmethod
    def parse_row(self, row: dict[str, str], employee_id: str, source_row: int) -> list[Punch]:
        """Turn one row into punches or raise ``RowError``."""

    @abstractmethod
    def row_identifier(self, row: dict[str, str]) -> str:
        """The store's own employee identifier for this row."""

    def row_date(self, row: dict[str, str]) -> Optional[date]:
        """Best-effort work date of a row, used to locate the day a bad row belongs to."""
        return None

    def safe_row_date(self, row: dict[str, str]) -> Optional[date]:
        try:
            return self.row_date(row)
        except (ValueError, IndexError, KeyError):
            return None

    def parse(self, file: FileLike, period: Optional[Period] = None) -> tuple[list[Punch], list[PayrollException]]:
        """Read the export. With a ``period``, punches are read from the day before its start to the day
        after its end, so shifts pair across both edges. Rows dated further out are skipped quietly, and
        bad or unknown rows are reported only when dated inside the period (or when the date is unreadable).
        """
        found: list[PayrollException] = []
        window = (period.start - timedelta(days=1), period.end + timedelta(days=1)) if period else None
        try:
            text = self._read(file)
        except (OSError, UnicodeDecodeError) as e:
            return [], [self._unreadable(f"cannot open file: {e}")]

        reader = csv.DictReader(io.StringIO(text))
        header = [h.strip() for h in (reader.fieldnames or [])]
        missing = [c for c in self.required_columns if c not in header]
        if missing:
            return [], [self._unreadable(
                f"expected columns {', '.join(self.required_columns)}; missing {', '.join(missing)}"
            )]
        reader.fieldnames = header
        extra = [col for col in header if col not in self.supported_columns]
        if extra:
            found.append(self._extra_columns_note(extra))

        punches: list[Punch] = []
        unknown: dict[str, list[int]] = defaultdict(list)
        for source_row, row in enumerate(reader, start=2):
            row = {k: (v or "").strip() for k, v in row.items() if k is not None}
            if not any(row.values()):
                continue
            on = self.safe_row_date(row)
            if window and on and not window[0] <= on <= window[1]:
                continue  # belongs to another pay period: skipped quietly
            in_period = period is None or on is None or period.start <= on <= period.end
            ident = self.row_identifier(row)
            employee_id = self.id_map.get(normalize_id(ident))
            if employee_id is None:
                if in_period:
                    unknown[ident].append(source_row)
                continue
            try:
                parsed = self.parse_row(row, employee_id, source_row)
                department = row.get(self.department_column, "") if self.department_column else ""
                punches.extend(replace(p, department=department or None) for p in parsed)
            except (RowError, ValueError) as e:
                if not in_period:
                    continue  # an unreadable punch next to the period cannot affect this payroll
                diagnoses = [d.to_dict() for d in getattr(e, "diagnoses", [])]
                # Severity is settled later by the HR stage, which knows the period and the schedule.
                found.append(exc.make(
                    "MALFORMED_ROW",
                    sentence(f"Store {self.location_code} row {source_row} ({', '.join(row.values())}): {e}"),
                    employee_id=employee_id,
                    location_code=self.location_code,
                    on=on,
                    resolution="Row skipped",
                    rows=[(self.location_code, source_row)],
                    context={"diagnosis": diagnoses} if diagnoses else None,
                ))
        for ident, source_rows in unknown.items():
            found.append(exc.make(
                "UNKNOWN_EMPLOYEE",
                f"Store {self.location_code}: identifier '{ident}' ({len(source_rows)} rows) does not match "
                f"any employee",
                location_code=self.location_code,
                rows=[(self.location_code, r) for r in source_rows],
            ))
        return punches, found

    def _read(self, file: FileLike) -> str:
        if isinstance(file, (str, Path)):
            with open(file, encoding="utf-8-sig", newline="") as fh:
                return fh.read()
        return file.read()

    def _extra_columns_note(self, columns: list[str]) -> PayrollException:
        """A file-level note, not a payroll exception: extra columns are ignored and never affect pay."""
        names = " and ".join(", ".join(f'"{c}"' for c in columns).rsplit(", ", 1))
        if len(columns) == 1:
            what = f"an extra column, {names}, that we don't use. It was ignored and doesn't"
        else:
            what = f"extra columns, {names}, that we don't use. They were ignored and don't"
        return exc.make(
            "UNKNOWN_COLUMN",
            f"Store {self.location_code}'s file has {what} affect pay. If the store changed its export "
            f"settings, it's worth a quick look.",
            location_code=self.location_code,
            resolution="Ignored",
            context={"columns": list(columns)},
        )

    def _unreadable(self, why: str) -> PayrollException:
        return exc.make(
            "FILE_UNREADABLE",
            f"Store {self.location_code} ({self.format_name}) file unreadable: {why}",
            location_code=self.location_code,
        )
