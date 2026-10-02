from django.urls import path

from . import views

app_name = "core"

urlpatterns = [
    path("", views.home, name="home"),
    path("runs/<int:run_id>/", views.overview, name="overview"),
    path("runs/<int:run_id>/inputs/", views.inputs, name="inputs"),
    path("runs/<int:run_id>/exceptions/", views.exceptions, name="exceptions"),
    path("runs/<int:run_id>/payroll/", views.payroll, name="payroll"),
    path("runs/<int:run_id>/employees/<str:employee_id>/", views.employee, name="employee"),
    path("runs/<int:run_id>/decide/", views.decide, name="decide"),
    path("runs/<int:run_id>/undo/", views.undo, name="undo"),
    path("runs/<int:run_id>/decisions/clear/", views.clear_decisions, name="clear_decisions"),
    path("runs/<int:run_id>/finalize/", views.finalize, name="finalize"),
    path("runs/<int:run_id>/reopen/", views.reopen, name="reopen"),
]
