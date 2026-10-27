from datetime import date

from django.core.management.base import BaseCommand, CommandError

from core import models as m
from core import services
from core.demo_data.generator import DEFAULT_PERIOD_START, DemoOptionError, PeriodStartError


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
        parser.add_argument("--extra-employees", type=int, default=0, metavar="N",
                            help="Add N clean filler employees (E19, E20, ...) on top of the 18 core employees")
        parser.add_argument("--stores", type=int, default=3, metavar="N",
                            help="Number of stores, at least 3 (default 3); extra stores D, E, ... reuse the "
                                 "three export formats in turn")
        parser.add_argument("--output", default=None, help="Directory for the generated files")

    def handle(self, *args, period, missing_store=None, output=None, extra_employees=0, stores=3, **options):
        try:
            seeded = services.seed_demo(missing_store=missing_store, output_dir=output,
                                        period_start=period_start(period), extra_employees=extra_employees,
                                        stores=stores)
        except (PeriodStartError, DemoOptionError) as e:
            raise CommandError(str(e))
        imports = ", ".join(f"{b.location.code} ({b.row_count} rows)" for b in seeded.imports.all())
        self.stdout.write(self.style.SUCCESS(
            f"Seeded period {seeded}: {m.Employee.objects.count()} employees, {m.Location.objects.count()} stores"))
        self.stdout.write(f"Time clock imports: {imports or 'none'}")
        if missing_store:
            self.stdout.write(self.style.WARNING(f"Store {missing_store.upper()} export left out on purpose"))
        self.stdout.write(f"Next: python manage.py run_payroll --period {seeded.start_date}")
