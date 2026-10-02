from django.urls import path, re_path

from . import views

app_name = "core"

# No run ids in URLs: every page resolves the visitor's own run (sandbox or baseline) from the session.
urlpatterns = [
    path("", views.overview, name="overview"),
    path("inputs/", views.inputs, name="inputs"),
    path("exceptions/", views.exceptions, name="exceptions"),
    path("payroll/", views.payroll, name="payroll"),
    path("employees/<str:employee_id>/", views.employee, name="employee"),
    path("decide/", views.decide, name="decide"),
    path("undo/", views.undo, name="undo"),
    path("decisions/clear/", views.clear_decisions, name="clear_decisions"),
    path("finalize/", views.finalize, name="finalize"),
    path("reopen/", views.reopen, name="reopen"),
    path("start-over/", views.start_over, name="start_over"),
    # Old /runs/<id>/... links: redirect to the visitor's own page; the id is never used.
    re_path(r"^runs/\d+/(?P<rest>.*)$", views.legacy_run_url),
]
