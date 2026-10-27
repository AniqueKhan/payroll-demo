"""Extra filler employees and extra stores on top of the fixed core demo."""
from collections import Counter
from decimal import Decimal as D
from io import StringIO

import pytest
from django.core.management import CommandError, call_command
from django.urls import reverse

from core.demo_data.generator import DemoOptionError, generate
from core.engine.inputs import load_inputs_from_dir
from core.engine.run import run_payroll
from core.models import Employee, Location, PayPeriod

LINE_FIELDS = ("regular_pay", "overtime_pay", "addons", "penalties", "absence_deductions", "loan_deductions",
               "custom_deductions", "gross", "net", "hours", "ot_hours")
CORE_IDS = {f"E{i:02d}" for i in range(1, 19)}


@pytest.fixture(scope="module")
def bigger(tmp_path_factory, rules):
    folder = tmp_path_factory.mktemp("bigger")
    manifest = generate(folder, rules=rules, extra_employees=50, stores=6)
    inputs = load_inputs_from_dir(folder, rules)
    return folder, manifest, inputs, run_payroll(inputs, rules)


def test_filler_adds_no_exceptions(bigger, result):
    *_, res = bigger
    assert Counter((e.key, e.severity) for e in res.exceptions) == \
        Counter((e.key, e.severity) for e in result.exceptions)
    assert not [e for e in res.exceptions if e.employee_id and e.employee_id not in CORE_IDS]
    assert [n.message for n in res.notes] == [n.message for n in result.notes]


def test_counts_and_totals(bigger, result):
    _, manifest, inputs, res = bigger
    assert len(inputs.employees) == 68 and len(res.lines) == 68
    assert [loc.code for loc in inputs.locations] == ["A", "B", "C", "D", "E", "F"]
    assert (manifest["stores"], manifest["extra_employees"]) == (6, 50)
    # Core employees are paid exactly as in the core demo.
    for line in result.lines:
        bigger_line = res.line_for(line.employee_id)
        assert tuple(getattr(bigger_line, f) for f in LINE_FIELDS) == tuple(getattr(line, f) for f in LINE_FIELDS)
    filler = [line for line in res.lines if line.employee_id not in CORE_IDS]
    for field in ("gross", "net", "regular_pay", "overtime_pay"):
        assert res.totals[field] == result.totals[field] + sum((getattr(x, field) for x in filler), D(0))
    assert all(x.overtime_pay == 0 and x.penalties == 0 and x.ot_hours == 0 for x in filler)
    # Store totals: the core stores gain their filler staff; every store adds up to the overall gross.
    assert sum(s["gross"] for s in res.totals["by_location"].values()) == res.totals["gross"]


def test_filler_staff_are_hourly_spread_round_robin_with_rates_in_range(bigger):
    _, _, inputs, _ = bigger
    filler = [e for e in inputs.employees if e.id not in CORE_IDS]
    assert [e.id for e in filler[:3]] == ["E19", "E20", "E21"] and filler[-1].id == "E68"
    assert [e.home_location for e in filler[:7]] == ["A", "B", "C", "D", "E", "F", "A"]
    assert all(e.pay_type == "hourly" for e in filler)
    assert all(D("15.00") <= rate <= D("18.00") for e in filler for rate in e.rates.values())
    names = [e.name for e in inputs.employees]
    assert len(names) == len(set(names))


def test_extra_stores_cycle_the_three_export_formats(bigger):
    folder, manifest, inputs, _ = bigger
    assert [(loc.code, loc.export_format) for loc in inputs.locations[3:]] == [
        ("D", "store_a"), ("E", "store_b"), ("F", "store_c")]
    assert manifest["time_files"]["F"] == "store_f_timeclock.csv"
    assert (folder / "store_d_timeclock.csv").read_text().startswith("EmpID,Date,Time,Type,Dept\n")
    assert (folder / "store_e_timeclock.csv").read_text().startswith("employee_code,punch_datetime,department\n")
    assert (folder / "store_f_timeclock.csv").read_text().startswith("Name,Date,In,Out,Dept\n")  # no Badge


def test_same_arguments_produce_identical_output(tmp_path, rules, bigger):
    folder, *_ = bigger
    generate(tmp_path, rules=rules, extra_employees=50, stores=6)
    for f in sorted(folder.iterdir()):
        assert (tmp_path / f.name).read_bytes() == f.read_bytes(), f.name


def test_missing_extra_store_is_blocking(tmp_path, rules):
    generate(tmp_path, rules=rules, extra_employees=10, stores=5, missing_store="E")
    res = run_payroll(load_inputs_from_dir(tmp_path, rules), rules)
    missing = [e for e in res.exceptions if e.code == "STORE_FILE_MISSING"]
    assert [(e.location_code, e.severity) for e in missing] == [("E", "blocking")]


@pytest.mark.parametrize("kwargs, message", [
    ({"stores": 2}, "between 3 and 26 stores, not 2"),
    ({"stores": 27}, "between 3 and 26 stores, not 27"),
    ({"extra_employees": -1}, "--extra-employees must be between 0 and 500"),
    ({"stores": 4, "missing_store": "Z"}, "--missing-store Z is not one of the stores (A, B, C, D)"),
])
def test_bad_options_are_rejected_and_write_nothing(tmp_path, rules, kwargs, message):
    with pytest.raises(DemoOptionError, match=message.replace("(", r"\(").replace(")", r"\)")):
        generate(tmp_path / "out", rules=rules, **kwargs)
    assert not (tmp_path / "out").exists()


@pytest.mark.django_db
def test_seed_demo_rejects_two_stores_and_seeds_nothing(tmp_path):
    with pytest.raises(CommandError, match="The demo needs between 3 and 26 stores, not 2. Stores A, B and C are "
                                           "always included"):
        call_command("seed_demo", "--stores", "2", "--output", str(tmp_path / "out"), stdout=StringIO())
    assert not PayPeriod.objects.exists() and not Employee.objects.exists()


@pytest.mark.django_db
def test_seed_with_options_and_screens(client, tmp_path):
    out = StringIO()
    call_command("seed_demo", "--period", "2026-10-05", "--extra-employees", "50", "--stores", "6",
                 "--output", str(tmp_path), stdout=out)
    assert "68 employees, 6 stores" in out.getvalue()
    call_command("run_payroll", "--period", "2026-10-05", stdout=StringIO())
    assert Employee.objects.count() == 68 and Location.objects.count() == 6

    inputs = client.get(reverse("core:inputs")).content.decode()
    for code in "ABCDEF":
        assert f'id="tab-{code}"' in inputs

    payroll = reverse("core:payroll")
    html = client.get(payroll).content.decode()
    assert html.count('class="rowlink"') == 68
    html = client.get(payroll, {"q": "e07"}).content.decode()
    assert html.count('class="rowlink"') == 1 and "Harper Lane" in html and "Showing 1 of 68 employees" in html
    html = client.get(payroll, {"q": "harper"}).content.decode()
    assert html.count('class="rowlink"') == 1
    html = client.get(payroll, {"store": "D"}).content.decode()
    d_staff = Employee.objects.filter(home_location__code="D").count()
    assert d_staff == 8 and html.count('class="rowlink"') == d_staff
    html = client.get(payroll, {"store": "A", "q": "nobody-like-this"}).content.decode()
    assert "No employees match" in html
