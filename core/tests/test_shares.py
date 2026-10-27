"""Percentage shares always add up to exactly 100.0%: the rounding leftover goes to the largest share."""
from decimal import Decimal as D

import pytest
from django.urls import reverse

from core import services
from core.demo_data.generator import generate
from core.engine.explain import percent_shares
from core.engine.inputs import load_inputs_from_dir
from core.engine.run import run_payroll


def shares(*weights):
    return [str(v) for v in percent_shares({f"k{i}": D(w) for i, w in enumerate(weights)}).values()]


@pytest.mark.parametrize("weights, expected", [
    (("1", "1", "1"), ["33.4", "33.3", "33.3"]),  # 99.9 rounded: +0.1 to the largest (first of equals)
    (("1",) * 6, ["16.5", "16.7", "16.7", "16.7", "16.7", "16.7"]),  # 100.2 rounded: -0.2
    (("1",) * 7, ["14.2", "14.3", "14.3", "14.3", "14.3", "14.3", "14.3"]),  # 100.1 rounded: -0.1
    (("2", "1"), ["66.7", "33.3"]),
    (("1", "2", "3"), ["16.7", "33.3", "50.0"]),
    (("1", "1", "998"), ["0.1", "0.1", "99.8"]),
    (("5",), ["100.0"]),
    (("0", "7"), ["0.0", "100.0"]),
    (("0", "0"), ["0.0", "0.0"]),  # nothing to share
])
def test_percent_shares(weights, expected):
    assert shares(*weights) == expected
    if any(D(w) for w in weights):
        assert sum(D(s) for s in expected) == D("100.0")


def test_default_demo_departments(result):
    by_dept = {k: v["gross"] for k, v in result.totals["by_department"].items()}
    # Rounded on their own these are 33.0 + 40.0 + 25.9 + 1.0 = 99.9; the 0.1 goes to Floor, the largest.
    assert percent_shares(by_dept) == {"Checkout": D("33.0"), "Floor": D("40.1"), "Stockroom": D("25.9"),
                                       "Unassigned": D("1.0")}


def test_bigger_demo_departments_add_up(tmp_path, rules):
    generate(tmp_path, rules=rules, extra_employees=60, stores=8)
    res = run_payroll(load_inputs_from_dir(tmp_path, rules), rules)
    by_dept = {k: v["gross"] for k, v in res.totals["by_department"].items()}
    assert sum(percent_shares(by_dept).values()) == D("100.0")


@pytest.mark.django_db
def test_screens_show_shares_that_add_up(client, tmp_path):
    period = services.seed_demo(output_dir=tmp_path)
    services.run_payroll(period)
    for page in ("overview", "payroll"):
        html = client.get(reverse(f"core:{page}")).content.decode()
        table = html.split("Labor cost by department")[1].split("</table>")[0]
        assert '<td class="num">40.1%</td>' in table
        assert '<td class="num">100.0%</td></tr></tfoot>' in table
