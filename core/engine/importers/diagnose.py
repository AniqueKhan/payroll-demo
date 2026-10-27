"""Plain-language explanations for values an importer can't read.

Each check looks for one kind of slip: a letter that looks like a digit, the wrong separator, a value that
can't exist, a missing part, or AM/PM in a 24-hour file. It marks the characters involved so the screens can
highlight them. "Did you mean ...?" is only offered when fixing the slips gives a valid value. When no
specific slip is found the reason falls back to the expected format.
"""
from __future__ import annotations

import calendar
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import Optional

# Letters that are easy to type (or OCR) instead of a digit.
LOOKALIKES = {"O": "0", "o": "0", "I": "1", "l": "1", "S": "5", "B": "8"}
AMPM = re.compile(r"\s*([AaPp]\.?\s?[Mm]\.?)$")


@dataclass
class Diagnosis:
    column: str
    value: str
    expected: str  # e.g. "HH:MM"
    example: str  # e.g. "09:00"
    reasons: list[str] = field(default_factory=list)
    marks: set[int] = field(default_factory=set)  # character positions in ``value`` to highlight
    suggestion: Optional[str] = None

    def add(self, reason: str, positions=()) -> None:
        if reason not in self.reasons:
            self.reasons.append(reason)
        self.marks.update(positions)

    def finish(self, fixed: Optional[str]) -> "Diagnosis":
        """``fixed`` is the corrected value when every slip found could be fixed, else None."""
        if not self.reasons:
            self.add(f"Doesn't match the expected format {self.expected}, for example {self.example}.")
        elif fixed is not None and fixed != self.value:
            self.suggestion = fixed
        return self

    def summary(self) -> str:
        text = f"{self.column} \"{self.value}\" can't be read. " + " ".join(self.reasons)
        return text + (f" Did you mean {self.suggestion}?" if self.suggestion else "")

    def to_dict(self) -> dict:
        return {"column": self.column, "value": self.value, "expected": self.expected, "reasons": self.reasons,
                "marks": sorted(self.marks), "suggestion": self.suggestion}


# ---------------------------------------------------------------- strict parsers (what the importers accept)

TIME24 = re.compile(r"(\d{1,2}):(\d{2})")


def parse_time24(value: str) -> time:
    """``HH:MM`` on a 24-hour clock; a one-digit hour (``9:30``) is accepted too."""
    m = TIME24.fullmatch(value)
    if not m:
        raise ValueError(value)
    return time(int(m.group(1)), int(m.group(2)))


def parse_date_iso(value: str) -> date:
    return date.fromisoformat(value)


def parse_datetime_12h(value: str) -> datetime:
    return datetime.strptime(value, "%m/%d/%Y %I:%M %p")


# ---------------------------------------------------------------- shared checks

def _lookalikes(d: Diagnosis, text: str, offset: int = 0) -> str:
    """Swap letters that look like digits; only in values that contain digits at all."""
    if not any(c.isdigit() for c in text):
        return text
    out = []
    for i, c in enumerate(text):
        if c in LOOKALIKES:
            d.add(f'"{c}" is a letter, not the digit {LOOKALIKES[c]}.', [offset + i])
            out.append(LOOKALIKES[c])
        else:
            out.append(c)
    return "".join(out)


def _separator(d: Diagnosis, sep: str, expected: str, where: str, position: int) -> None:
    if sep != expected:
        shown = "a space" if sep == " " else f'"{sep}"'
        d.add(f'Uses {shown} between {where} instead of "{expected}".', [position])


def _span(start: int, end: int) -> range:
    return range(start, end)


def _clock(d: Diagnosis, h: int, mi: int, h_span: range, mi_span: range, twelve_hour: bool) -> bool:
    ok = True
    if twelve_hour and not 1 <= h <= 12:
        d.add(f"Hour {h} doesn't exist on a 12-hour clock: hours run from 1 to 12.", h_span)
        ok = False
    elif not twelve_hour and h > 23:
        d.add(f"Hour {h} doesn't exist: hours run from 00 to 23.", h_span)
        ok = False
    if mi > 59:
        d.add(f"Minute {mi} doesn't exist: minutes run from 00 to 59.", mi_span)
        ok = False
    return ok


def _calendar_date(d: Diagnosis, y: int, mo: int, dd: int, mo_span: range, dd_span: range) -> Optional[date]:
    if not 1 <= mo <= 12:
        d.add(f"Month {mo} doesn't exist: months run from 01 to 12.", mo_span)
        return None
    try:
        return date(y, mo, dd)
    except ValueError:
        d.add(f"{calendar.month_name[mo]} {y} has no day {dd}.", dd_span)
        return None


def _time_parts(d: Diagnosis, fixed: str, offset: int, sep: str = ":"):
    """Split a fixed-up time into hour and minute, reporting missing parts. Returns (h, mi, h_span, mi_span)."""
    m = re.fullmatch(r"(\d{1,2})(\D)(\d{2})", fixed)
    if m:
        _separator(d, m.group(2), sep, "the hours and the minutes", offset + m.start(2))
        return (int(m.group(1)), int(m.group(3)), _span(offset + m.start(1), offset + m.end(1)),
                _span(offset + m.start(3), offset + m.end(3)))
    if re.fullmatch(r"\d{3,4}", fixed):
        d.add(f'The "{sep}" between the hours and the minutes is missing.')
        cut = len(fixed) - 2
        return int(fixed[:cut]), int(fixed[cut:]), _span(offset, offset + cut), _span(offset + cut, offset + len(fixed))
    if re.fullmatch(r"\d{1,2}\D\d", fixed):
        d.add("The minutes need two digits.", [offset + len(fixed) - 1])
    elif re.fullmatch(r"\d{1,2}\D?", fixed):
        d.add("The minutes are missing.")
    elif re.fullmatch(r"\D\d{2}", fixed):
        d.add("The hour is missing.")
    return None


# ---------------------------------------------------------------- one diagnoser per kind of value

def diagnose_time24(column: str, value: str) -> Diagnosis:
    """A 24-hour ``HH:MM`` time (store_a ``Time``, store_c ``In``/``Out``)."""
    d = Diagnosis(column, value, "HH:MM", "09:00")
    core, pm = value, None
    m = AMPM.search(value)
    if m and any(c.isdigit() for c in value[:m.start()]):
        core, pm = value[:m.start()], m.group(1).lower().startswith("p")
        d.add(f'Has "{m.group(1)}", but this file uses 24-hour times (for example 18:30, not 6:30 PM).',
              _span(m.start(1), m.end(1)))
    parts = _time_parts(d, _lookalikes(d, core), 0)
    if parts is None:
        return d.finish(None)
    h, mi, h_span, mi_span = parts
    if pm is not None:
        if not _clock(d, h, mi, h_span, mi_span, twelve_hour=True):
            return d.finish(None)
        h = h % 12 + (12 if pm else 0)
    if not _clock(d, h, mi, h_span, mi_span, twelve_hour=False):
        return d.finish(None)
    return d.finish(f"{h:02d}:{mi:02d}")


def diagnose_date_iso(column: str, value: str) -> Diagnosis:
    """A ``YYYY-MM-DD`` date (store_a and store_c ``Date``)."""
    d = Diagnosis(column, value, "YYYY-MM-DD", "2026-09-07")
    fixed = _lookalikes(d, value)
    m = re.fullmatch(r"(\d{4})(\D)(\d{1,2})(\D)(\d{1,2})", fixed)
    if m:
        for g in (2, 4):
            _separator(d, m.group(g), "-", "the year, month and day", m.start(g))
        y, mo, dd = int(m.group(1)), int(m.group(3)), int(m.group(5))
        spans = _span(*m.span(3)), _span(*m.span(5))
    elif re.fullmatch(r"\d{8}", fixed):
        d.add('The "-" between the year, month and day is missing.')
        y, mo, dd = int(fixed[:4]), int(fixed[4:6]), int(fixed[6:])
        spans = _span(4, 6), _span(6, 8)
    else:
        if re.fullmatch(r"\d{4}\D\d{1,2}\D?", fixed):
            d.add("The day is missing.")
        return d.finish(None)
    on = _calendar_date(d, y, mo, dd, *spans)
    return d.finish(on.isoformat() if on else None)


def diagnose_datetime_12h(column: str, value: str) -> Diagnosis:
    """A ``MM/DD/YYYY hh:mm AM`` date and time (store_b ``punch_datetime``)."""
    d = Diagnosis(column, value, "MM/DD/YYYY hh:mm AM", "09/07/2026 09:00 AM")
    m = AMPM.search(value)
    core = value[:m.start()] if m else value
    pm: Optional[bool] = None
    if m:
        pm = m.group(1).lower().startswith("p")
        if m.group(1).upper() not in ("AM", "PM"):
            d.add(f'Writes "{m.group(1)}" instead of {"PM" if pm else "AM"}.', _span(m.start(1), m.end(1)))
    fixed = _lookalikes(d, core)
    tokens = re.fullmatch(r"(\S+)(\s+)(\S+)", fixed)
    if not tokens:
        if re.fullmatch(r"\d{1,2}\D\d{1,2}\D\d{4}", fixed):
            d.add("The time is missing.")
        elif re.fullmatch(r"\d{1,2}:\d{2}", fixed):
            d.add("The date is missing.")
        return d.finish(None)
    date_m = re.fullmatch(r"(\d{1,2})(\D)(\d{1,2})(\D)(\d{4})", tokens.group(1))
    on = None
    if date_m:
        for g in (2, 4):
            _separator(d, date_m.group(g), "/", "the month, day and year", date_m.start(g))
        on = _calendar_date(d, int(date_m.group(5)), int(date_m.group(1)), int(date_m.group(3)),
                            _span(*date_m.span(1)), _span(*date_m.span(3)))
    parts = _time_parts(d, tokens.group(3), tokens.start(3))
    if not date_m or parts is None:
        return d.finish(None)
    h, mi, h_span, mi_span = parts
    if pm is None:
        if h > 12 and _clock(d, h, mi, h_span, mi_span, twelve_hour=False):
            d.add(f"{h}:{mi:02d} is a 24-hour time, but this file uses AM/PM.")
            pm, h = True, h - 12
        else:
            d.add("The AM or PM at the end is missing.")
            return d.finish(None)
    if not _clock(d, h, mi, h_span, mi_span, twelve_hour=True) or on is None:
        return d.finish(None)
    return d.finish(f"{on:%m/%d/%Y} {h:02d}:{mi:02d} {'PM' if pm else 'AM'}")
