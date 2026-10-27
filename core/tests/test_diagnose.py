"""Plain-language explanations for unreadable values, per store format."""
import io

import pytest
from django.urls import reverse

from core import services
from core.engine.importers import StoreAImporter, StoreBImporter, StoreCImporter
from core.engine.importers.diagnose import diagnose_date_iso, diagnose_datetime_12h, diagnose_time24
from core.models import RawImportRow

from .helpers import only

FALLBACK_TIME = "Doesn't match the expected format HH:MM, for example 09:00."
FALLBACK_DATE = "Doesn't match the expected format YYYY-MM-DD, for example 2026-09-07."
FALLBACK_B = "Doesn't match the expected format MM/DD/YYYY hh:mm AM, for example 09/07/2026 09:00 AM."
LETTER_O = '"O" is a letter, not the digit 0.'


@pytest.mark.parametrize("value, reasons, marks, suggestion", [
    # Lookalikes: O -> 0, l/I -> 1, S -> 5, B -> 8.
    ("18:3O", [LETTER_O], [4], "18:30"),
    ("l8:30", ['"l" is a letter, not the digit 1.'], [0], "18:30"),
    ("I8:30", ['"I" is a letter, not the digit 1.'], [0], "18:30"),
    ("1S:30", ['"S" is a letter, not the digit 5.'], [1], "15:30"),
    ("1B:00", ['"B" is a letter, not the digit 8.'], [1], "18:00"),
    # A lookalike that, once fixed, still gives an impossible time: two reasons and no suggestion.
    ("9:6O", [LETTER_O, "Minute 60 doesn't exist: minutes run from 00 to 59."], [2, 3], None),
    # Wrong separator.
    ("18.30", ['Uses "." between the hours and the minutes instead of ":".'], [2], "18:30"),
    ("18 30", ['Uses a space between the hours and the minutes instead of ":".'], [2], "18:30"),
    # Impossible values.
    ("25:00", ["Hour 25 doesn't exist: hours run from 00 to 23."], [0, 1], None),
    ("18:75", ["Minute 75 doesn't exist: minutes run from 00 to 59."], [3, 4], None),
    # Missing part.
    ("1830", ['The ":" between the hours and the minutes is missing.'], [], "18:30"),
    ("18:", ["The minutes are missing."], [], None),
    (":30", ["The hour is missing."], [], None),
    ("18:3", ["The minutes need two digits."], [3], None),
    # AM/PM in a 24-hour file.
    ("6:30 PM", ['Has "PM", but this file uses 24-hour times (for example 18:30, not 6:30 PM).'], [5, 6], "18:30"),
    ("9:15am", ['Has "am", but this file uses 24-hour times (for example 18:30, not 6:30 PM).'], [4, 5], "09:15"),
    # Nothing specific: the expected format.
    ("noon", [FALLBACK_TIME], [], None),
    ("", [FALLBACK_TIME], [], None),
])
def test_time24(value, reasons, marks, suggestion):
    d = diagnose_time24("Time", value)
    assert (d.reasons, sorted(d.marks), d.suggestion) == (reasons, marks, suggestion)


@pytest.mark.parametrize("value, reasons, suggestion", [
    ("2026-O9-09", [LETTER_O], "2026-09-09"),
    ("2026/09/09", ['Uses "/" between the year, month and day instead of "-".'], "2026-09-09"),
    ("2026-02-30", ["February 2026 has no day 30."], None),
    ("2026-13-01", ["Month 13 doesn't exist: months run from 01 to 12."], None),
    ("20260909", ['The "-" between the year, month and day is missing.'], "2026-09-09"),
    ("2026-09", ["The day is missing."], None),
    ("09/09/2026", [FALLBACK_DATE], None),
])
def test_date_iso(value, reasons, suggestion):
    d = diagnose_date_iso("Date", value)
    assert (d.reasons, d.suggestion) == (reasons, suggestion)


@pytest.mark.parametrize("value, reasons, suggestion", [
    ("09/09/2026 05:3O PM", [LETTER_O], "09/09/2026 05:30 PM"),
    ("09/09/2026 9:6O AM", [LETTER_O, "Minute 60 doesn't exist: minutes run from 00 to 59."], None),
    ("09.09.2026 05:00 PM", ['Uses "." between the month, day and year instead of "/".'], "09/09/2026 05:00 PM"),
    ("09/31/2026 05:00 PM", ["September 2026 has no day 31."], None),
    ("09/09/2026 13:00 PM", ["Hour 13 doesn't exist on a 12-hour clock: hours run from 1 to 12."], None),
    ("09/09/2026 17:00", ["17:00 is a 24-hour time, but this file uses AM/PM."], "09/09/2026 05:00 PM"),
    ("09/09/2026 05:00", ["The AM or PM at the end is missing."], None),
    ("09/09/2026", ["The time is missing."], None),
    ("whenever", [FALLBACK_B], None),
])
def test_datetime_12h(value, reasons, suggestion):
    d = diagnose_datetime_12h("punch_datetime", value)
    assert (d.reasons, d.suggestion) == (reasons, suggestion)


def test_summary_reads_as_sentences():
    assert diagnose_time24("Out", "18:3O").summary() == 'Out "18:3O" can\'t be read. "O" is a letter, not the digit ' \
                                                        "0. Did you mean 18:30?"


# ------------------------------------------------------------------ importers

@pytest.mark.parametrize("importer, text, diagnosed", [
    (StoreAImporter, "EmpID,Date,Time,Type\n1002,2026-09-09,18:3O,OUT\n", [("Time", "18:3O", "18:30")]),
    (StoreAImporter, "EmpID,Date,Time,Type\n1002,2026/09/09,9:6O,IN\n",
     [("Date", "2026/09/09", "2026-09-09"), ("Time", "9:6O", None)]),
    (StoreBImporter, "employee_code,punch_datetime\nRV-202,09/09/2026 05:3O PM\n",
     [("punch_datetime", "09/09/2026 05:3O PM", "09/09/2026 05:30 PM")]),
    (StoreCImporter, "Name,Date,In,Out\nSage Delgado,2026-09-09,09:00,18:3O\n", [("Out", "18:3O", "18:30")]),
])
def test_importers_explain_each_unreadable_value(inputs, importer, text, diagnosed):
    code = {"store_a": "A", "store_b": "B", "store_c": "C"}[importer.format_name]
    punches, found = importer(code, inputs.employees).parse(io.StringIO(text))
    bad = only(found)
    assert punches == [] and bad.code == "MALFORMED_ROW"
    assert [(d["column"], d["value"], d["suggestion"]) for d in bad.context["diagnosis"]] == diagnosed
    for column, value, suggestion in diagnosed:
        assert f'{column} "{value}" can\'t be read.' in bad.message
        assert (f"Did you mean {suggestion}?" in bad.message) == bool(suggestion)


def test_one_digit_hour_is_read(inputs):
    punches, found = StoreAImporter("A", inputs.employees).parse(io.StringIO(
        "EmpID,Date,Time,Type\n1002,2026-09-09,9:30,IN\n"))
    assert found == [] and only(punches).timestamp.hour == 9


def test_e02_review_item_carries_the_explanation(result):
    ex = only(result.exceptions_for("MALFORMED_ROW", "E02"))
    assert ex.message.startswith('Store A row 37 (1002, 2026-09-09, 9:6O, IN, Checkout): Time "9:6O" can\'t be read. '
                                 "\"O\" is a letter, not the digit 0. Minute 60 doesn't exist: minutes run from 00 "
                                 "to 59. Paid 8.00 h from the remaining punches")
    assert only(ex.context["diagnosis"]) == {
        "column": "Time", "value": "9:6O", "expected": "HH:MM", "marks": [2, 3], "suggestion": None,
        "reasons": [LETTER_O, "Minute 60 doesn't exist: minutes run from 00 to 59."]}


# ------------------------------------------------------------------ screens

@pytest.mark.django_db
def test_inputs_and_exceptions_highlight_the_bad_characters(client, tmp_path):
    period = services.seed_demo(output_dir=tmp_path)
    services.run_payroll(period)
    raw = RawImportRow.objects.get(batch__location__code="A", row_number=37)
    assert only(raw.diagnosis)["marks"] == [2, 3]
    highlighted = '<code class="mono bad-value"'
    inputs = client.get(reverse("core:inputs")).content.decode()
    row = inputs.split('id="row-A-37"')[1].split("</tr>")[0]
    assert highlighted + ' title="Can\'t be read">9:<mark>6O</mark></code>' in row
    assert "&quot;O&quot; is a letter, not the digit 0." in row and "Minute 60 doesn&#x27;t exist" in row
    assert "Expected format: <code class=\"mono\">HH:MM</code>" in row and "Did you mean" not in row
    queue = client.get(reverse("core:exceptions")).content.decode()
    card = queue.split('id="exc-MALFORMED_ROW-E02-2026-09-09-A"')[1].split("</article>")[0]
    assert f"{highlighted}>9:<mark>6O</mark></code> can't be read:" in card


def test_mark_chars_escapes_and_groups():
    from core.templatetags.payroll_ui import mark_chars

    assert mark_chars("<1:3O", [0, 3]) == "<mark>&lt;</mark>1:<mark>3</mark>O"
    assert mark_chars("9:6O", [2, 3]) == "9:<mark>6O</mark>"
