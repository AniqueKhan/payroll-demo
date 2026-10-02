from collections import Counter
from datetime import date
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError

from core import models as m
from core import services
from core.engine.explain import fmt_money


class Command(BaseCommand):
    help = "Run payroll for a period as a draft and print a summary."

    def add_arguments(self, parser):
        parser.add_argument("--period", required=True, help="Period start date, YYYY-MM-DD")
        parser.add_argument("--rules", default=None, help="Rules file (defaults to core/rules/demo_rules.yaml)")
        parser.add_argument("--top", type=int, default=5, help="How many lines to print with their trails")

    def handle(self, *args, period, rules=None, top=5, **options):
        try:
            pay_period = m.PayPeriod.objects.get(start_date=date.fromisoformat(period))
        except (ValueError, m.PayPeriod.DoesNotExist):
            raise CommandError(f"No pay period starting {period}. Run seed_demo first.")
        try:
            run = services.run_payroll(pay_period, rules)
        except services.PayrollError as e:
            raise CommandError(str(e))

        out = self.stdout
        out.write(self.style.MIGRATE_HEADING(f"Payroll run {run.pk} for {pay_period} ({run.status})"))
        totals = run.totals
        out.write(f"Employees: {totals['employees']}  Gross: {fmt_money(totals['gross'])}  "
                  f"Net: {fmt_money(totals['net'])}")

        out.write(self.style.MIGRATE_HEADING("\nTotals per store"))
        names = dict(m.Location.objects.values_list("code", "name"))
        for code, slot in sorted(totals["by_location"].items()):
            out.write(f"  {code} {names.get(code, ''):<18} {Decimal(slot['hours']):>8} h  {fmt_money(slot['gross']):>12}")

        out.write(self.style.MIGRATE_HEADING("\nExceptions by severity"))
        severities = Counter(run.exceptions.values_list("severity", flat=True))
        for sev in ("blocking", "needs_review", "info"):
            out.write(f"  {sev:<13} {severities.get(sev, 0)}")
        out.write(self.style.MIGRATE_HEADING("\nExceptions"))
        for x in run.exceptions.select_related("employee", "location").order_by("id"):
            who = x.employee.employee_id if x.employee else "-"
            style = {"blocking": self.style.ERROR, "needs_review": self.style.WARNING}.get(x.severity, str)
            out.write(style(f"  [{x.severity}] {x.code} {who} {x.date or ''}: {x.message}"))

        out.write(self.style.MIGRATE_HEADING(f"\nTop {top} lines by gross"))
        for line in run.lines.select_related("employee").order_by("-gross")[:top]:
            out.write(f"\n  {line.employee.employee_id} {line.employee.name}: gross {fmt_money(line.gross)}, "
                      f"net {fmt_money(line.net)}")
            for step in line.trail:
                if (step.get("refs") or {}).get("step") != "shift":
                    out.write(f"    - {step['label']}: {step['formula']}")

        blockers = services.finalize_blockers(run)
        out.write("")
        if blockers:
            out.write(self.style.WARNING("Cannot finalize yet: " + "; ".join(blockers)))
        else:
            out.write(self.style.SUCCESS("Ready to finalize"))
