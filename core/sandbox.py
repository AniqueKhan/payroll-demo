"""Per-visitor sandboxes, tracked in the Django session.

A visitor who only browses never gets a sandbox: they see the baseline run. The first action (a POST)
creates their private copy. The session only stores the sandbox id; everything else is server-side.
"""
from __future__ import annotations

from typing import Optional

from django.core.exceptions import ValidationError
from django.utils import timezone

from . import models as m
from . import services

SESSION_KEY = "sandbox_id"


def get_sandbox(request, create: bool = False) -> Optional[m.DemoSandbox]:
    """The visitor's sandbox, or None. ``create=True`` makes one (and remembers it in the session).

    A session pointing at a sandbox that was cleaned up is treated as a new visitor.
    """
    sandbox = None
    sandbox_id = request.session.get(SESSION_KEY)
    if sandbox_id:
        try:
            sandbox = m.DemoSandbox.objects.filter(pk=sandbox_id).first()
        except (ValidationError, ValueError):
            sandbox = None
        if sandbox is None:
            del request.session[SESSION_KEY]
    if sandbox is None and create:
        period = services.demo_period()
        if period is None:
            return None
        sandbox = services.create_sandbox(period)
        request.session[SESSION_KEY] = str(sandbox.pk)
    elif sandbox is not None and request.method == "POST":
        sandbox.last_seen_at = timezone.now()
        sandbox.save(update_fields=["last_seen_at"])
    return sandbox


def forget_sandbox(request) -> None:
    """Delete the visitor's sandbox (runs and decisions cascade) and clear the session key."""
    sandbox = get_sandbox(request)
    if sandbox is not None:
        sandbox.delete()
    request.session.pop(SESSION_KEY, None)


def current_run(request, period: m.PayPeriod) -> Optional[m.PayrollRun]:
    """The visitor's sandbox run if they have one, otherwise the baseline run."""
    sandbox = get_sandbox(request)
    if sandbox is not None:
        return services.ensure_sandbox_run(period, sandbox)
    return services.current_run(period)
