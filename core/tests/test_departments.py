"""Labor cost by department: taken from each shift's clock-in row, exact to the cent."""
import io
from dataclasses import replace
from decimal import Decimal as D

import pytest
from django.urls import reverse

from core import services
from core.demo_data.generator import generate
from core.engine.importers import StoreAImporter, StoreBImporter, StoreCImporter
from core.engine.inputs import load_inputs_from_dir
from core.engine.run import run_payroll
from core.models import PayrollLineRecord

from .helpers import only

MONEY = ("regular_pay", "overtime_pay", "addons", "absence_deductions", "gross")


def assert_departments_add_up(res):
    totals = res.totals
    for line in res.lines:
        assert set(line.by_department) == set(line.by_location), line.employee_id
        for store, bucket in line.by_location.items():
            depts = line.by_department[store].values()
            for field in ("hours",) + MONEY:
                assert sum((d[field] for d in depts), D(0)) == bucket[field], (line.employee_id, store, field)
    for store, depts in totals["by_location_department"].items():
        assert sum((d["gross"] for d in depts.values()), D(0)) == totals["by_location"][store]["gross"], store
        assert sum((d["hours"] for d in depts.values()), D(0)) == totals["by_location"][store]["hours"], store
    assert sum((d["gross"] for d in totals["by_department"].values()), D(0)) == totals["gross"]


def test_department_totals_add_up_to_store_totals_and_gross(result):
    assert_departments_add_up(result)
    assert set(result.totals["by_department"]) == {"Checkout", "Floor", "Stockroom", "Unassigned"}
    assert result.totals["gross"] == D("24441.99")


def test_department_totals_add_up_with_extra_staff_and_stores(tmp_path, rules):
    generate(tmp_path, rules=rules, extra_employees=40, stores=7)
    assert_departments_add_up(run_payroll(load_inputs_from_dir(tmp_path, rules), rules))


def test_shift_without_a_department_is_unassigned(result, scenario):
    s = scenario(20, employee="E18")
    line = result.line_for("E18")
    assert list(line.by_department["A"]) == ["Unassigned"]  # Store A's clock left the column blank
    assert list(line.by_department["C"]) == ["Checkout"]
    assert line.by_department["A"]["Unassigned"]["hours"] == line.by_location["A"]["hours"] > 0
    assert s["departments"] == {"A": ["Unassigned"], "C": ["Checkout"]}


def test_export_without_a_department_column_is_unassigned(inputs, rules):
    # Store B's export as it was before the column existed: everything lands in Unassigned at Store B.
    lines = inputs.time_files["B"].read_text().splitlines()
    without = "\n".join(",".join(row.split(",")[:2]) for row in lines) + "\n"
    res = run_payroll(replace(inputs, time_files={**inputs.time_files, "B": io.StringIO(without)}), rules)
    store_b = res.totals["by_location_department"]["B"]
    # Everything punched at Store B is Unassigned. E10's shift punched at Store A (Floor) but paid at Store B
    # (scenario 11) keeps the department of its clock-in row.
    assert list(store_b) == ["Floor", "Unassigned"]
    assert store_b["Floor"]["hours"] == D("8.00")
    assert res.line_for("E10").by_department["B"]["Floor"]["hours"] == D("8.00")
    assert not [n for n in res.notes if n.location_code == "B"]


def test_two_department_employees(result, scenario):
    e06, e15 = result.line_for("E06"), result.line_for("E15")
    assert {s: list(d) for s, d in e06.by_department.items()} == scenario(20, employee="E06")["departments"]
    assert list(e15.by_department["C"]) == ["Checkout", "Floor"]
    assert e15.by_department["C"]["Floor"]["hours"] == D("45.00")  # week 1
    assert e15.by_department["C"]["Checkout"]["hours"] == D("40.00")  # week 2


@pytest.mark.parametrize("importer, text", [
    (StoreAImporter, "EmpID,Date,Time,Type,Dept\n1002,2026-09-07,09:00,IN,Floor\n1002,2026-09-07,17:00,OUT,Stockroom\n"),
    (StoreBImporter, "employee_code,punch_datetime,department\nRV-202,09/07/2026 09:00 AM,Floor\n"
                     "RV-202,09/07/2026 05:00 PM,Stockroom\n"),
    (StoreCImporter, "Name,Date,In,Out,Dept\nSage Delgado,2026-09-07,09:00,17:00,Floor\n"),
])
def test_department_comes_from_the_clock_in_row(inputs, rules, importer, text):
    from core.engine.hr_stage import build_shifts

    code = {"store_a": "A", "store_b": "B", "store_c": "C"}[importer.format_name]
    punches, found = importer(code, inputs.employees).parse(io.StringIO(text))
    assert found == []  # the department column is supported, so there is no extra-column note
    shift = only(build_shifts(punches, rules))
    assert shift.department == "Floor"


def test_the_planted_extra_column_is_now_badge(result):
    note = only(result.notes)
    assert note.context == {"columns": ["Badge"]} and '"Badge"' in note.message


@pytest.mark.django_db
def test_screens_show_labor_cost_by_department(client, tmp_path):
    period = services.seed_demo(output_dir=tmp_path)
    run = services.run_payroll(period)
    for page in ("overview", "payroll"):
        html = client.get(reverse(f"core:{page}")).content.decode()
        assert "Labor cost by department" in html and "Labor cost by store and department" in html
        for dept in ("Floor", "Stockroom", "Checkout", "Unassigned"):
            assert f"<td>{dept}</td>" in html
        assert "Store A total" in html
    line = PayrollLineRecord.objects.get(run=run, employee__employee_id="E06")
    assert set(line.by_department) == {"A", "B"} and list(line.by_department["B"]) == ["Stockroom"]
    employee = client.get(reverse("core:employee", kwargs={"employee_id": "E06"})).content.decode()
    assert "By store and department" in employee and "Stockroom" in employee
    # A visitor's sandbox stores the same breakdown.
    client.post(reverse("core:decide"), {"exception_key": "MISSED_CLOCK_OUT|E03|2026-09-10|A",
                                         "action": "approve_with_value", "hours": "8"})
    mine = PayrollLineRecord.objects.exclude(run=run).get(employee__employee_id="E03")
    assert D(mine.by_department["A"]["Stockroom"]["gross"]) == mine.gross
