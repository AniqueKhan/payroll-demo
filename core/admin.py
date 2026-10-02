from django.contrib import admin

from . import models as m


@admin.register(m.Employee)
class EmployeeAdmin(admin.ModelAdmin):
    list_display = ("employee_id", "name", "home_location", "pay_type", "hire_date", "exit_date")
    list_filter = ("home_location", "pay_type")


@admin.register(m.PayrollRun)
class PayrollRunAdmin(admin.ModelAdmin):
    list_display = ("id", "period", "status", "created_at", "finalized_at")


@admin.register(m.PayrollExceptionRecord)
class PayrollExceptionRecordAdmin(admin.ModelAdmin):
    list_display = ("code", "severity", "status", "employee", "location", "date")
    list_filter = ("severity", "status", "code")


for model in (m.Location, m.PayPeriod, m.ScheduledShift, m.LeaveRecord, m.PayAddOn, m.LoanAdvance,
              m.ImportBatch, m.PayrollLineRecord):
    admin.site.register(model)
