"""Demo screens: overview, raw inputs, review queue, payroll and one employee's calculation.

No login. One shared payroll run. Every POST re-runs payroll, so run ids change; a request for
a run that no longer exists is sent to the same page of the latest run.
"""
from collections import defaultdict
from decimal import Decimal
from functools import wraps

from django.contrib import messages
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from . import models as m
from . import services
from .engine.config import DEFAULT_RULES_PATH, load_rules
from .templatetags.payroll_ui import code_title

SEVERITY_RANK = {"blocking": 0, "needs_review": 1, "info": 2}
STAGES = [("hr", "HR stage"), ("finance", "Finance stage"), ("custom", "Custom rules and net pay")]


def anchor(key: str) -> str:
    return "exc-" + "".join(c if c.isalnum() or c in "-_" else "-" for c in key)


def with_run(view):
    """Resolve ``run_id``; a stale id (the run was replaced by a re-run) goes to the latest run."""
    @wraps(view)
    def wrapper(request, run_id, **kwargs):
        run = m.PayrollRun.objects.select_related("period").filter(pk=run_id).first()
        if run is None:
            latest = services.latest_run()
            if latest is None:
                return redirect("core:home")
            if request.method == "POST":
                return view(request, latest, **kwargs)  # e.g. a second tab: act on the current run
            return redirect(reverse(request.resolver_match.view_name, kwargs={"run_id": latest.pk, **kwargs}))
        return view(request, run, **kwargs)
    return wrapper


def base_context(run: m.PayrollRun, active: str) -> dict:
    return {
        "run": run,
        "period": run.period,
        "active": active,
        "open_count": run.exceptions.filter(severity="needs_review", status="open").count(),
        "blocking_count": run.exceptions.filter(severity="blocking").count(),
        "is_final": run.status == m.PayrollRun.FINALIZED,
    }


def raw_row_index(period: m.PayPeriod) -> dict[tuple[str, int], m.RawImportRow]:
    rows = m.RawImportRow.objects.filter(batch__period=period).select_related("batch__location")
    return {(r.batch.location.code, r.row_number): r for r in rows}


# ---------------------------------------------------------------- pages

def home(request):
    run = services.latest_run()
    if run is None:
        return render(request, "core/empty.html")
    return redirect("core:overview", run_id=run.pk)


@with_run
def overview(request, run):
    ctx = base_context(run, "overview")
    totals = run.totals
    names = dict(m.Location.objects.values_list("code", "name"))
    severities = defaultdict(int)
    for sev in run.exceptions.values_list("severity", flat=True):
        severities[sev] += 1
    ctx.update({
        "totals": totals,
        "stores": [{"code": code, "name": names.get(code, ""), "hours": slot["hours"], "gross": slot["gross"]}
                   for code, slot in sorted(totals["by_location"].items())],
        "severity_counts": [("blocking", severities["blocking"]), ("needs_review", severities["needs_review"]),
                            ("info", severities["info"])],
        "approved_count": run.exceptions.filter(severity="needs_review", status="approved").count(),
        "blockers": services.finalize_blockers(run),
        "decision_count": run.period.decisions.count(),
    })
    return render(request, "core/overview.html", ctx)


@with_run
def inputs(request, run):
    ctx = base_context(run, "inputs")
    referenced: dict[tuple[str, int], list[m.PayrollExceptionRecord]] = defaultdict(list)
    for x in run.exceptions.all():
        for loc, row in x.rows:
            referenced[(loc, row)].append(x)
    batches = {b.location.code: b for b in run.period.imports.select_related("location")}
    missing = {x.location.code: x for x in run.exceptions.filter(code="STORE_FILE_MISSING").select_related("location")}
    stores = []
    for loc in m.Location.objects.all():
        batch = batches.get(loc.code)
        rows = []
        if batch:
            for r in batch.raw_rows.all():
                refs = sorted(referenced.get((loc.code, r.row_number), []), key=lambda x: SEVERITY_RANK[x.severity])
                rows.append({
                    "number": r.row_number,
                    "values": [r.raw.get(c, "") for c in batch.columns],
                    "error": r.parse_error,
                    "refs": [{"title": code_title(x.code), "anchor": anchor(x.exception_key), "record": x}
                             for x in refs],
                    "severity": refs[0].severity if refs else "",
                })
        stores.append({"location": loc, "batch": batch, "rows": rows, "missing": missing.get(loc.code),
                       "flagged": sum(1 for r in rows if r["refs"] or r["error"])})
    ctx.update({"stores": stores})
    return render(request, "core/inputs.html", ctx)


def present_exception(x: m.PayrollExceptionRecord, decisions: dict, raw_rows: dict, leave_types: list[str]) -> dict:
    kind = services.value_kind(x) if x.severity == "needs_review" else None
    decision = decisions.get(x.exception_key)
    ctx = x.context or {}
    default = {
        "hours": ctx.get("suggested_hours", ""),
        "location": ctx.get("scheduled", ""),
        "amount": ctx.get("deducted", ""),
        "leave_type": leave_types[0] if leave_types else "",
    }.get(kind or "", "")
    return {
        "record": x,
        "title": code_title(x.code),
        "anchor": anchor(x.exception_key),
        "rows": [{"location": loc, "number": n, "raw": raw_rows.get((loc, n))} for loc, n in x.rows],
        "decision": decision,
        "value_kind": kind,
        "default": default,
        "stores": ctx.get("stores", []),
    }


@with_run
def exceptions(request, run):
    ctx = base_context(run, "exceptions")
    decisions = {d.exception_key: d for d in run.period.decisions.all()}
    raw_rows = raw_row_index(run.period)
    leave_types = list(load_rules(DEFAULT_RULES_PATH).leave_types)
    records = run.exceptions.select_related("employee", "location").order_by("id")
    items = [present_exception(x, decisions, raw_rows, leave_types) for x in records]
    ctx.update({
        "sections": [
            ("blocking", "Blocking", "Payroll cannot be finalized until the input is fixed and payroll is re-run.",
             [i for i in items if i["record"].severity == "blocking"]),
            ("needs_review", "Needs review", "A person decides. Approve as is, or approve with a corrected value.",
             [i for i in items if i["record"].severity == "needs_review" and i["record"].status == "open"]),
            ("approved", "Reviewed", "Decided by a reviewer. Decisions survive re-runs; undo to reopen an item.",
             [i for i in items if i["record"].severity == "needs_review" and i["record"].status != "open"]),
            ("info", "Handled by rule", "Settled automatically by a rule in the rules file. Shown for the record.",
             [i for i in items if i["record"].severity == "info"]),
        ],
        "leave_types": leave_types,
        "decision_count": len(decisions),
    })
    return render(request, "core/exceptions.html", ctx)


def employee_markers(run: m.PayrollRun) -> dict[str, dict]:
    """Worst exception state per employee, for the payroll table."""
    order = {"blocking": 0, "open": 1, "approved": 2, "info": 3}
    labels = {"blocking": "Blocking", "open": "Needs review", "approved": "Reviewed", "info": "Info"}
    worst: dict[str, tuple[str, int]] = {}
    for x in run.exceptions.exclude(employee=None).select_related("employee"):
        state = "blocking" if x.severity == "blocking" else (
            "info" if x.severity == "info" else ("open" if x.status == "open" else "approved"))
        emp = x.employee.employee_id
        current, count = worst.get(emp, ("info", 0))
        worst[emp] = (min(current, state, key=order.get), count + 1)
    return {emp: {"state": s, "label": labels[s], "count": n} for emp, (s, n) in worst.items()}


@with_run
def payroll(request, run):
    ctx = base_context(run, "payroll")
    lines = list(run.lines.select_related("employee__home_location"))
    if request.GET.get("sort") == "gross":
        lines.sort(key=lambda line: line.gross, reverse=True)
    fields = ["regular_hours", "overtime_hours", "regular_pay", "overtime_pay", "addons", "deductions", "gross",
              "net"]
    totals = {f: sum((getattr(line, f) for line in lines), Decimal(0)) for f in fields}
    ctx.update({"lines": lines, "totals": totals, "markers": employee_markers(run),
                "sort": request.GET.get("sort", "")})
    return render(request, "core/payroll.html", ctx)


@with_run
def employee(request, run, employee_id):
    ctx = base_context(run, "payroll")
    emp = get_object_or_404(m.Employee.objects.select_related("home_location"), employee_id=employee_id)
    line = run.lines.filter(employee=emp).first()
    if line is None:
        raise Http404("No payroll line for this employee in this run")
    sections = []
    for stage, title in STAGES:
        steps = [s for s in line.trail if s.get("stage", "hr") == stage]
        sections.append({
            "title": title,
            "shifts": [s for s in steps if (s.get("refs") or {}).get("step") == "shift"],
            "steps": [s for s in steps if (s.get("refs") or {}).get("step") != "shift"],
        })
    names = dict(m.Location.objects.values_list("code", "name"))
    exceptions_ = [
        {"record": x, "title": code_title(x.code), "anchor": anchor(x.exception_key)}
        for x in run.exceptions.filter(employee=emp).order_by("id")
    ]
    ctx.update({
        "employee": emp,
        "line": line,
        "rates": [(code, names.get(code, ""), rate) for code, rate in sorted(emp.rates.items())],
        "sections": sections,
        "by_location": [(code, names.get(code, ""), b) for code, b in sorted(line.by_location.items())],
        "employee_exceptions": exceptions_,
        "adjustment_count": sum(1 for s in line.trail if (s.get("refs") or {}).get("adjustment")),
    })
    return render(request, "core/employee.html", ctx)


# ---------------------------------------------------------------- actions (POST, CSRF protected)

def _after_change(request, new_run: m.PayrollRun, message: str, fragment: str = ""):
    open_count = new_run.exceptions.filter(severity="needs_review", status="open").count()
    left = "Nothing left to review." if open_count == 0 else f"{open_count} item{'s' if open_count != 1 else ''} " \
                                                             f"still need{'s' if open_count == 1 else ''} review."
    messages.success(request, f"{message} {left}")
    url = reverse("core:exceptions", kwargs={"run_id": new_run.pk})
    return redirect(f"{url}#{fragment}" if fragment else url)


@require_POST
@with_run
def decide(request, run):
    key = request.POST.get("exception_key", "")
    action = request.POST.get("action", "")
    value = {k: request.POST.get(k, "") for k in ("hours", "location", "leave_type", "amount")}
    try:
        new_run = services.decide(run.period, key, action, value, request.POST.get("note", ""))
    except services.PayrollError as e:
        messages.error(request, str(e))
        return redirect(reverse("core:exceptions", kwargs={"run_id": run.pk}) + f"#{anchor(key)}")
    title = code_title(key.split("|")[0])
    verb = "Approved with value" if action == m.ReviewDecision.APPROVE_WITH_VALUE else "Approved"
    return _after_change(request, new_run, f"{verb}: {title}.", anchor(key))


@require_POST
@with_run
def undo(request, run):
    key = request.POST.get("exception_key", "")
    try:
        new_run = services.undo_decision(run.period, key)
    except services.PayrollError as e:
        messages.error(request, str(e))
        return redirect("core:exceptions", run_id=run.pk)
    return _after_change(request, new_run, f"Decision undone: {code_title(key.split('|')[0])}.", anchor(key))


@require_POST
@with_run
def clear_decisions(request, run):
    try:
        new_run = services.clear_decisions(run.period)
    except services.PayrollError as e:
        messages.error(request, str(e))
        return redirect("core:exceptions", run_id=run.pk)
    return _after_change(request, new_run, "All decisions reset.")


@require_POST
@with_run
def finalize(request, run):
    try:
        services.finalize(run)
    except services.FinalizeRefused as e:
        messages.error(request, "Cannot finalize: " + "; ".join(e.reasons))
    except services.PayrollError as e:
        messages.error(request, str(e))
    else:
        messages.success(request, "Payroll finalized. Loan and advance deductions were posted.")
    return redirect("core:overview", run_id=run.pk)


@require_POST
@with_run
def reopen(request, run):
    try:
        services.reopen(run)
    except services.PayrollError as e:
        messages.error(request, str(e))
    else:
        messages.success(request, "Payroll reopened as a draft. Loan postings were taken back.")
    return redirect("core:overview", run_id=run.pk)
