"""Demo screens: overview, raw inputs, review queue, payroll and one employee's calculation.

No login, no run ids in URLs. Each visitor sees the shared baseline run until their first action (a POST),
which creates their private sandbox; from then on every page shows their own run. See ``core.sandbox``.
"""
import re
from collections import defaultdict
from decimal import Decimal
from functools import wraps

from django.contrib import messages
from django.db.models import Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from . import labels
from . import models as m
from . import services
from .sandbox import current_run, forget_sandbox, get_sandbox
from .templatetags.payroll_ui import code_title

SEVERITY_RANK = {"blocking": 0, "needs_review": 1, "attendance": 2, "info": 3}
STAGES = [("hr", "HR stage"), ("finance", "Finance stage"), ("custom", "Custom rules and net before taxes")]


def anchor(key: str) -> str:
    return "exc-" + "".join(c if c.isalnum() or c in "-_" else "-" for c in key)


def with_run(view):
    """Resolve the visitor's run: their sandbox run if they have one, otherwise the baseline."""
    @wraps(view)
    def wrapper(request, **kwargs):
        period = services.demo_period()
        run = current_run(request, period) if period else None
        if run is None:
            return render(request, "core/empty.html")
        return view(request, run, **kwargs)
    return wrapper


def base_context(run: m.PayrollRun, active: str) -> dict:
    return {
        "run": run,
        "period": run.period,
        "active": active,
        "in_sandbox": run.sandbox_id is not None,
        "open_count": run.exceptions.filter(severity="needs_review", status="open").count(),
        "blocking_count": run.exceptions.filter(severity="blocking").count(),
        "attendance_count": run.exceptions.filter(severity="attendance").count(),
        "is_final": run.status == m.PayrollRun.FINALIZED,
    }


def period_label(period: m.PayPeriod) -> str:
    """'Sep 7 to 20', or 'Sep 28 to Oct 11' when the period spans two months."""
    return labels.period_label(period.start_date, period.end_date)


def department_context(run: m.PayrollRun) -> dict:
    """Labor cost by department, overall and per store, from the run totals."""
    totals = run.totals
    names = dict(m.Location.objects.values_list("code", "name"))
    gross = Decimal(totals.get("gross", "0"))
    by_dept = totals.get("by_department", {})

    def rows(depts: dict) -> list[dict]:
        return [{"name": name, "hours": Decimal(v["hours"]), "gross": Decimal(v["gross"])}
                for name, v in depts.items()]

    departments = rows(by_dept)
    for d in departments:
        d["share"] = f"{(d['gross'] / gross * 100):.1f}" if gross else "0.0"
    store_departments = []
    for code, depts in totals.get("by_location_department", {}).items():
        dept_rows = rows(depts)
        store_departments.append({"code": code, "name": names.get(code, ""), "departments": dept_rows,
                                  "hours": sum((d["hours"] for d in dept_rows), Decimal(0)),
                                  "gross": sum((d["gross"] for d in dept_rows), Decimal(0))})
    return {"departments": departments, "store_departments": store_departments,
            "department_hours": sum((d["hours"] for d in departments), Decimal(0)),
            "department_gross": sum((d["gross"] for d in departments), Decimal(0))}


def raw_row_index(period: m.PayPeriod, wanted: set[tuple[str, int]]) -> dict[tuple[str, int], m.RawImportRow]:
    """The raw rows referenced by exceptions, keyed by (store, row number). Loads only those rows."""
    by_store: dict[str, set[int]] = defaultdict(set)
    for loc, number in wanted:
        by_store[loc].add(number)
    query = Q(pk__in=[])
    for loc, numbers in by_store.items():
        query |= Q(batch__location__code=loc, row_number__in=sorted(numbers))
    rows = m.RawImportRow.objects.filter(query, batch__period=period).select_related("batch__location")
    return {(r.batch.location.code, r.row_number): r for r in rows}


# ---------------------------------------------------------------- pages

LEGACY_PAGE = re.compile(r"^(?:(inputs|exceptions|payroll)/|employees/([A-Za-z0-9_-]+)/)?$")


def legacy_run_url(request, rest=""):
    """Old ``/runs/<id>/...`` links go to the same page of the visitor's own view. The id is ignored."""
    match = LEGACY_PAGE.match(rest)
    if not match:
        return redirect("core:overview")
    page, employee_id = match.groups()
    if employee_id:
        return redirect("core:employee", employee_id=employee_id)
    return redirect(f"core:{page}" if page else "core:overview")


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
                            ("info", severities["info"]), ("attendance", severities["attendance"])],
        "approved_count": run.exceptions.filter(severity="needs_review", status="approved").count(),
        "blockers": services.finalize_blockers(run),
        "decision_count": services.decisions(run.period, run.sandbox).count(),
        **department_context(run),
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
    notes = defaultdict(list)
    for note in run.file_notes:
        notes[note["location"]].append(note)
    stores = []
    for loc in m.Location.objects.all():
        batch = batches.get(loc.code)
        rows = []
        skipped = 0
        if batch:
            skipped = batch.raw_rows.filter(in_period=False).count()
            for r in batch.raw_rows.filter(in_period=True):
                refs = sorted(referenced.get((loc.code, r.row_number), []), key=lambda x: SEVERITY_RANK[x.severity])
                bad = {d["column"]: d for d in r.diagnosis}
                rows.append({
                    "number": r.row_number,
                    "cells": [{"value": r.raw.get(c, ""), "diagnosis": bad.get(c)} for c in batch.columns],
                    "diagnosis": r.diagnosis,
                    "error": r.parse_error,
                    "refs": [{"title": code_title(x.code), "anchor": anchor(x.exception_key), "record": x}
                             for x in refs],
                    "severity": refs[0].severity if refs else "",
                })
        stores.append({"location": loc, "batch": batch, "rows": rows, "missing": missing.get(loc.code),
                       "flagged": sum(1 for r in rows if r["refs"] or r["error"]), "skipped": skipped,
                       "notes": notes.get(loc.code, [])})
    ctx.update({"stores": stores, "period_label": period_label(run.period)})
    return render(request, "core/inputs.html", ctx)


def present_exception(x: m.PayrollExceptionRecord, decisions: dict, raw_rows: dict) -> dict:
    kind = services.value_kind(x) if x.severity in ("needs_review", "attendance") else None
    decision = decisions.get(x.exception_key)
    ctx = x.context or {}
    default = {
        "hours": ctx.get("suggested_hours", ""),
        "location": ctx.get("default", ctx.get("scheduled", "")),
        "amount": ctx.get("deducted", ""),
        "leave": "paid",
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
        "choices": ctx.get("choices", []),
    }


@with_run
def exceptions(request, run):
    ctx = base_context(run, "exceptions")
    decisions = {d.exception_key: d for d in services.decisions(run.period, run.sandbox)}
    records = list(run.exceptions.select_related("employee", "location").order_by("id"))
    raw_rows = raw_row_index(run.period, {(loc, n) for x in records for loc, n in x.rows})
    items = [present_exception(x, decisions, raw_rows) for x in records]
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
            ("attendance", "Attendance to discuss",
             "These don't change anyone's pay. They're here so you can follow up with the employee.",
             [i for i in items if i["record"].severity == "attendance"]),
        ],
        "decision_count": len(decisions),
    })
    return render(request, "core/exceptions.html", ctx)


def employee_markers(run: m.PayrollRun) -> dict[str, dict]:
    """Worst exception state per employee, for the payroll table."""
    order = {"blocking": 0, "open": 1, "approved": 2, "attendance": 3, "info": 4}
    names = {"blocking": "Blocking", "open": "Needs review", "approved": "Reviewed", "attendance": "To discuss",
             "info": "Info"}
    worst: dict[str, tuple[str, int]] = {}
    for x in run.exceptions.exclude(employee=None).select_related("employee"):
        if x.severity in ("blocking", "info", "attendance"):
            state = x.severity
        else:
            state = "open" if x.status == "open" else "approved"
        emp = x.employee.employee_id
        current, count = worst.get(emp, ("info", 0))
        worst[emp] = (min(current, state, key=order.get), count + 1)
    return {emp: {"state": s, "label": names[s], "count": n} for emp, (s, n) in worst.items()}


@with_run
def payroll(request, run):
    ctx = base_context(run, "payroll")
    q = request.GET.get("q", "").strip()
    store = request.GET.get("store", "").strip().upper()
    all_lines = run.lines.select_related("employee__home_location")
    lines = all_lines
    if q:
        lines = lines.filter(Q(employee__name__icontains=q) | Q(employee__employee_id__iexact=q)
                             | Q(employee__employee_id__icontains=q))
    if store:
        lines = lines.filter(employee__home_location__code=store)
    lines = list(lines)
    if request.GET.get("sort") == "gross":
        lines.sort(key=lambda line: line.gross, reverse=True)
    fields = ["regular_hours", "overtime_hours", "regular_pay", "overtime_pay", "addons", "deductions", "gross",
              "net"]
    totals = {f: sum((getattr(line, f) for line in lines), Decimal(0)) for f in fields}
    ctx.update({"lines": lines, "totals": totals, "markers": employee_markers(run),
                "sort": request.GET.get("sort", ""), "q": q, "store": store,
                "stores": m.Location.objects.all(), "total_count": all_lines.count(),
                "filtered": bool(q or store), **department_context(run)})
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
        "by_location": [(code, names.get(code, ""), b, sorted((line.by_department or {}).get(code, {}).items()))
                        for code, b in sorted(line.by_location.items())],
        "employee_exceptions": exceptions_,
        "adjustment_count": sum(1 for s in line.trail if (s.get("refs") or {}).get("adjustment")),
    })
    return render(request, "core/employee.html", ctx)


# ---------------------------------------------------------------- actions (POST, CSRF protected)
# Every action works on the visitor's own sandbox. Actions that change anything create it on first use.

def _after_change(request, new_run: m.PayrollRun, message: str, fragment: str = ""):
    open_count = new_run.exceptions.filter(severity="needs_review", status="open").count()
    left = "Nothing left to review." if open_count == 0 else f"{open_count} item{'s' if open_count != 1 else ''} " \
                                                             f"still need{'s' if open_count == 1 else ''} review."
    messages.success(request, f"{message} {left}")
    url = reverse("core:exceptions")
    return redirect(f"{url}#{fragment}" if fragment else url)


def _no_sandbox(request, message: str):
    messages.info(request, message)
    return redirect("core:exceptions")


@require_POST
def decide(request):
    key = request.POST.get("exception_key", "")
    action = request.POST.get("action", "")
    value = {k: request.POST.get(k, "") for k in ("hours", "location", "amount", "leave")}
    sandbox = get_sandbox(request, create=True)
    if sandbox is None:
        return redirect("core:overview")
    try:
        new_run = services.decide(services.demo_period(), key, action, value, request.POST.get("note", ""),
                                  sandbox=sandbox)
    except services.PayrollError as e:
        messages.error(request, str(e))
        return redirect(reverse("core:exceptions") + f"#{anchor(key)}")
    title = code_title(key.split("|")[0])
    if value["leave"] and key.startswith("ABSENCE_UNINFORMED|"):
        return _after_change(request, new_run, f"Recorded as {value['leave']} leave: {title}.", anchor(key))
    verb = "Approved with value" if action == m.ReviewDecision.APPROVE_WITH_VALUE else "Approved"
    return _after_change(request, new_run, f"{verb}: {title}.", anchor(key))


@require_POST
def undo(request):
    key = request.POST.get("exception_key", "")
    sandbox = get_sandbox(request)
    if sandbox is None:
        return _no_sandbox(request, "Nothing to undo: you have not made any decisions yet.")
    try:
        new_run = services.undo_decision(services.demo_period(), key, sandbox=sandbox)
    except services.PayrollError as e:
        messages.error(request, str(e))
        return redirect("core:exceptions")
    return _after_change(request, new_run, f"Decision undone: {code_title(key.split('|')[0])}.", anchor(key))


@require_POST
def clear_decisions(request):
    sandbox = get_sandbox(request)
    if sandbox is None:
        return _no_sandbox(request, "Nothing to reset: you have not made any decisions yet.")
    try:
        new_run = services.clear_decisions(services.demo_period(), sandbox=sandbox)
    except services.PayrollError as e:
        messages.error(request, str(e))
        return redirect("core:exceptions")
    return _after_change(request, new_run, "All decisions reset.")


@require_POST
def finalize(request):
    sandbox = get_sandbox(request, create=True)
    if sandbox is None:
        return redirect("core:overview")
    try:
        services.finalize(services.demo_period(), sandbox=sandbox)
    except services.FinalizeRefused as e:
        messages.error(request, "Cannot finalize: " + "; ".join(e.reasons))
    except services.PayrollError as e:
        messages.error(request, str(e))
    else:
        messages.success(request, "Payroll finalized in your copy of the demo.")
    return redirect("core:overview")


@require_POST
def reopen(request):
    sandbox = get_sandbox(request)
    if sandbox is None:
        messages.info(request, "Nothing to reopen.")
        return redirect("core:overview")
    try:
        services.reopen(services.demo_period(), sandbox=sandbox)
    except services.PayrollError as e:
        messages.error(request, str(e))
    else:
        messages.success(request, "Payroll reopened as a draft.")
    return redirect("core:overview")


@require_POST
def start_over(request):
    forget_sandbox(request)
    messages.success(request, "Started over: you are back on the clean demo.")
    return redirect("core:overview")
