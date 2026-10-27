from datetime import date

from django.core.management.base import BaseCommand, CommandError

from core import services
from core.demo_data.generator import DEFAULT_PERIOD_START, PeriodStartError


def period_start(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise CommandError(f"--period must be a date like 2026-09-07, not {value!r}")


class Command(BaseCommand):
    help = "Generate the synthetic demo inputs and load them into the database (replaces existing demo data)."

    def add_arguments(self, parser):
        parser.add_argument("--period", metavar="YYYY-MM-DD", default=DEFAULT_PERIOD_START.isoformat(),
                            help="Period start date; must fall on the rules' week start day "
                                 f"(default {DEFAULT_PERIOD_START.isoformat()})")
        parser.add_argument("--missing-store", metavar="CODE", default=None,
                            help="Leave this store's time clock export out (scenario 2)")
        parser.add_argument("--output", default=None, help="Directory for the generated files")

    def handle(self, *args, period, missing_store=None, output=None, **options):
        try:
            seeded = services.seed_demo(missing_store=missing_store, output_dir=output,
                                        period_start=period_start(period))
        except PeriodStartError as e:
            raise CommandError(str(e))
        imports = ", ".join(f"{b.location.code} ({b.row_count} rows)" for b in seeded.imports.all())
        self.stdout.write(self.style.SUCCESS(f"Seeded period {seeded}"))
        self.stdout.write(f"Time clock imports: {imports or 'none'}")
        if missing_store:
            self.stdout.write(self.style.WARNING(f"Store {missing_store.upper()} export left out on purpose"))
        self.stdout.write(f"Next: python manage.py run_payroll --period {seeded.start_date}")
