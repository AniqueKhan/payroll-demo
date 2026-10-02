"""Importer interface: turn one store's time clock export into normalized ``Punch`` objects.

Importers never raise on bad data. Unreadable files, unexpected columns, bad rows and
unknown identifiers all come back as exceptions next to the punches that did parse.
"""
from __future__ import annotations

import csv
import io
from abc import ABC, abstractmethod
from collections import Counter
from pathlib import Path
from datetime import date
from typing import IO, Iterable, Optional, Union

from .. import exceptions as exc
from ..types import Employee, PayrollException, Punch

FileLike = Union[str, Path, IO[str]]


class RowError(ValueError):
    """A row that cannot be turned into punches."""


def normalize_id(value: str) -> str:
    return " ".join(value.split()).casefold()


class Importer(ABC):
    format_name: str = ""
    required_columns: tuple[str, ...] = ()

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

    def parse(self, file: FileLike) -> tuple[list[Punch], list[PayrollException]]:
        found: list[PayrollException] = []
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
        for col in header:
            if col not in self.required_columns:
                found.append(exc.make(
                    "UNKNOWN_COLUMN",
                    f"Store {self.location_code}: column '{col}' is not part of the {self.format_name} format",
                    location_code=self.location_code,
                    resolution="Column ignored",
                ))

        punches: list[Punch] = []
        unknown: Counter[str] = Counter()
        for source_row, row in enumerate(reader, start=2):
            row = {k: (v or "").strip() for k, v in row.items() if k is not None}
            if not any(row.values()):
                continue
            ident = self.row_identifier(row)
            employee_id = self.id_map.get(normalize_id(ident))
            if employee_id is None:
                unknown[ident] += 1
                continue
            try:
                punches.extend(self.parse_row(row, employee_id, source_row))
            except (RowError, ValueError) as e:
                try:
                    on = self.row_date(row)
                except (ValueError, IndexError):
                    on = None
                # Severity is settled later by the HR stage, which knows the period and the schedule.
                found.append(exc.make(
                    "MALFORMED_ROW",
                    f"Store {self.location_code} row {source_row}: {e} ({', '.join(row.values())})",
                    employee_id=employee_id,
                    location_code=self.location_code,
                    on=on,
                    resolution="Row skipped",
                ))
        for ident, count in unknown.items():
            found.append(exc.make(
                "UNKNOWN_EMPLOYEE",
                f"Store {self.location_code}: identifier '{ident}' ({count} rows) does not match any employee",
                location_code=self.location_code,
            ))
        return punches, found

    def _read(self, file: FileLike) -> str:
        if isinstance(file, (str, Path)):
            with open(file, encoding="utf-8-sig", newline="") as fh:
                return fh.read()
        return file.read()

    def _unreadable(self, why: str) -> PayrollException:
        return exc.make(
            "FILE_UNREADABLE",
            f"Store {self.location_code} ({self.format_name}) file unreadable: {why}",
            location_code=self.location_code,
        )
