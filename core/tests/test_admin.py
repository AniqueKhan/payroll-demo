"""Admin readability: every model has a plain-language name, list columns, filters and search."""
import pytest
from django.apps import apps
from django.contrib import admin
from django.urls import reverse

from core import services
from core.models import (
    DemoSandbox, Employee, ImportBatch, LeaveRecord, LoanAdvance, Location, PayAddOn, PayPeriod,
    PayrollExceptionRecord, PayrollLineRecord, PayrollRun, RawImportRow, ReviewDecision, ScheduledShift,
)

pytestmark = pytest.mark.django_db

CORE_MODELS = list(apps.get_app_config("core").get_models())


@pytest.fixture
def seeded(tmp_path):
    period = services.seed_demo(output_dir=tmp_path)
    services.run_payroll(period)
    sandbox = services.create_sandbox(period)
    services.decide(period, "MISSED_CLOCK_OUT|E03|2026-09-10|A", "approve_with_value", {"hours": "8"},
                    sandbox=sandbox)
    services.decide(period, "LOAN_CAPPED|E16|2026-09-20|", "approve", sandbox=sandbox)
    return period


def test_every_model_has_a_readable_str(seeded):
    for model in CORE_MODELS:
        objects = list(model.objects.all()[:50])
        assert objects, f"no {model.__name__} seeded"
        for obj in objects:
            text = str(obj)
            assert "object (" not in text and text.strip(), (model.__name__, text)


def test_readable_examples(seeded):
    def one(model, **kw):
        return str(model.objects.get(**kw))

    assert one(PayrollLineRecord, run__sandbox=None, employee__employee_id="E07") == \
        "E07 Harper Lane · Sep 7 to 20 · gross $1,980.00 · net $1,865.90"
    assert one(ScheduledShift, employee__employee_id="E03", date="2026-09-10") == \
        "E03 Casey Lin · Store A · Thu Sep 10 · 09:00 to 17:00"
    assert one(LeaveRecord, employee__employee_id="E09") == "E09 Kai Bennett · Sep 14 · sick leave (paid)"
    assert one(PayAddOn, employee__employee_id="E15") == "E15 Riley Okafor · commission $90.00 · Sep 10"
    assert one(LoanAdvance, employee__employee_id="E16") == "E16 Sage Delgado · loan $2,000.00 · $400.00 per period"
    assert one(ImportBatch, location__code="C") == "Store C · store_c_timeclock.csv · Sep 7 to 20 · 56 rows"
    raw = RawImportRow.objects.get(batch__location__code="A", raw__Time="9:6O")
    assert str(raw) == f"Store A · row {raw.row_number} · 1002, 2026-09-09, 9:6O, IN"
    assert one(Location, code="A") == "Store A · Northgate Market"
    assert one(Employee, employee_id="E07") == "E07 Harper Lane"
    assert one(PayPeriod) == "Sep 7 to 20, 2026"
    assert one(PayrollRun, sandbox=None) == "Baseline · Sep 7 to 20, 2026 · draft"
    sandbox = DemoSandbox.objects.get()
    assert one(PayrollRun, sandbox=sandbox).startswith(f"Visitor copy {sandbox.short_id} · Sep 7 to 20, 2026 · ")
    assert str(sandbox).startswith(f"Visitor copy {sandbox.short_id} · last seen ")
    assert one(PayrollExceptionRecord, run__sandbox=None, code="MISSED_CLOCK_OUT") == \
        "Missed clock-out · E03 Casey Lin · Store A · Sep 10 · needs review (open)"
    assert one(PayrollExceptionRecord, run__sandbox=sandbox, code="MISSED_CLOCK_OUT").endswith(
        "needs review (approved)")
    assert one(ReviewDecision, exception_key__startswith="MISSED") == \
        f"Missed clock-out · E03 · Store A · Sep 10 · approved with 8.00 h · visitor copy {sandbox.short_id}"
    assert one(ReviewDecision, exception_key__startswith="LOAN").startswith(
        "Loan deduction capped · E16 · Sep 20 · approved as is")


def test_every_model_is_registered_with_columns_filters_and_search():
    for model in CORE_MODELS:
        model_admin = admin.site._registry.get(model)
        assert model_admin is not None, f"{model.__name__} is not in the admin"
        assert model_admin.list_display != ("__str__",), model.__name__
        assert model_admin.search_fields, model.__name__


@pytest.mark.parametrize("model", CORE_MODELS, ids=lambda m: m.__name__)
def test_admin_lists_render_without_n_plus_one(admin_client, seeded, model, django_assert_max_num_queries):
    url = reverse(f"admin:core_{model._meta.model_name}_changelist")
    with django_assert_max_num_queries(15):  # a fixed budget whatever the row count (up to 100 per page)
        assert admin_client.get(url).status_code == 200
    assert admin_client.get(url, {"q": "E03"}).status_code == 200


def test_employee_search_finds_by_name_and_id(admin_client, seeded):
    url = reverse("admin:core_payrolllinerecord_changelist")
    for q in ("E07", "Harper"):
        html = admin_client.get(url, {"q": q}).content.decode()
        assert "Harper Lane" in html and "Casey Lin" not in html
