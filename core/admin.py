"""Django admin for the payroll demo: readable lists, filters and search for every model."""
from django.contrib import admin
from django.db.models import Count

from . import labels
from . import models as m

EMPLOYEE_SEARCH = ("employee__employee_id", "employee__name")


@admin.register(m.Location)
class LocationAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "export_format", "timezone")
    list_filter = ("export_format",)
    search_fields = ("code", "name")


@admin.register(m.Employee)
class EmployeeAdmin(admin.ModelAdmin):
    list_display = ("employee_id", "name", "home_location", "pay_type", "hire_date", "exit_date")
    list_filter = ("home_location", "pay_type")
    search_fields = ("employee_id", "name")
    list_select_related = ("home_location",)


@admin.register(m.PayPeriod)
class PayPeriodAdmin(admin.ModelAdmin):
    list_display = ("__str__", "start_date", "end_date")
    search_fields = ("start_date",)


@admin.register(m.ScheduledShift)
class ScheduledShiftAdmin(admin.ModelAdmin):
    list_display = ("employee", "location", "date", "start", "end")
    list_filter = ("location", "employee__pay_type")
    search_fields = EMPLOYEE_SEARCH
    date_hierarchy = "date"
    list_select_related = ("employee", "location")


@admin.register(m.LeaveRecord)
class LeaveRecordAdmin(admin.ModelAdmin):
    list_display = ("employee", "date", "leave_type", "paid", "informed")
    list_filter = ("leave_type", "paid", "informed", "employee__home_location")
    search_fields = EMPLOYEE_SEARCH
    list_select_related = ("employee",)


@admin.register(m.PayAddOn)
class PayAddOnAdmin(admin.ModelAdmin):
    list_display = ("employee", "date", "kind", "amount", "include_in_regular_rate")
    list_filter = ("kind", "include_in_regular_rate", "employee__home_location")
    search_fields = EMPLOYEE_SEARCH
    list_select_related = ("employee",)


@admin.register(m.LoanAdvance)
class LoanAdvanceAdmin(admin.ModelAdmin):
    list_display = ("employee", "kind", "total", "installment", "paid_to_date", "start_period")
    list_filter = ("kind", "employee__home_location")
    search_fields = EMPLOYEE_SEARCH
    list_select_related = ("employee",)


@admin.register(m.ImportBatch)
class ImportBatchAdmin(admin.ModelAdmin):
    list_display = ("location", "basename", "period", "status", "row_count", "imported_at")
    list_filter = ("location", "period", "status")
    search_fields = ("filename", "location__code", "location__name")
    list_select_related = ("location", "period")


@admin.register(m.RawImportRow)
class RawImportRowAdmin(admin.ModelAdmin):
    list_display = ("store", "row_number", "values", "in_period", "parse_error")
    list_filter = ("batch__location", "batch__period", "in_period")
    search_fields = ("batch__filename", "batch__location__code", "parse_error")
    list_select_related = ("batch__location",)

    @admin.display(description="Store", ordering="batch__location__code")
    def store(self, obj):
        return f"Store {obj.batch.location.code}"

    @admin.display(description="Row as exported")
    def values(self, obj):
        return ", ".join(str(v) for v in obj.raw.values())


@admin.register(m.DemoSandbox)
class DemoSandboxAdmin(admin.ModelAdmin):
    list_display = ("__str__", "created_at", "last_seen_at", "decision_count")
    search_fields = ("id",)
    date_hierarchy = "last_seen_at"

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(n_decisions=Count("decisions"))

    @admin.display(description="Decisions", ordering="n_decisions")
    def decision_count(self, obj):
        return obj.n_decisions


@admin.register(m.PayrollRun)
class PayrollRunAdmin(admin.ModelAdmin):
    list_display = ("__str__", "period", "owner", "status", "created_at", "finalized_at")
    list_filter = ("status", "period", ("sandbox", admin.EmptyFieldListFilter))
    search_fields = ("sandbox__id",)
    list_select_related = ("period", "sandbox")

    @admin.display(description="Belongs to", ordering="sandbox")
    def owner(self, obj):
        return f"Visitor copy {obj.sandbox.short_id}" if obj.sandbox_id else "Baseline"


@admin.register(m.PayrollLineRecord)
class PayrollLineRecordAdmin(admin.ModelAdmin):
    list_display = ("employee", "run", "hours", "overtime_hours", "gross", "net")
    list_filter = ("run__period", "run__status", "employee__home_location", "employee__pay_type")
    search_fields = EMPLOYEE_SEARCH
    list_select_related = ("employee", "run__period", "run__sandbox")


@admin.register(m.PayrollExceptionRecord)
class PayrollExceptionRecordAdmin(admin.ModelAdmin):
    list_display = ("title", "employee", "location", "date", "severity", "status", "run")
    list_filter = ("severity", "status", "code", "location", "run__period", "run__status")
    search_fields = EMPLOYEE_SEARCH + ("code", "message")
    list_select_related = ("employee", "location", "run__period", "run__sandbox")

    @admin.display(description="Exception", ordering="code")
    def title(self, obj):
        return labels.code_title(obj.code)


@admin.register(m.ReviewDecision)
class ReviewDecisionAdmin(admin.ModelAdmin):
    list_display = ("__str__", "period", "action", "note", "decided_at")
    list_filter = ("action", "period", ("sandbox", admin.EmptyFieldListFilter))
    search_fields = ("exception_key", "note")
    list_select_related = ("period", "sandbox")
