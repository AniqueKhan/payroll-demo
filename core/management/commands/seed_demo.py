from django.core.management.base import BaseCommand

from core import services


class Command(BaseCommand):
    help = "Generate the synthetic demo inputs and load them into the database (replaces existing demo data)."

    def add_arguments(self, parser):
        parser.add_argument("--missing-store", metavar="CODE", default=None,
                            help="Leave this store's time clock export out (scenario 2)")
        parser.add_argument("--output", default=None, help="Directory for the generated files")

    def handle(self, *args, missing_store=None, output=None, **options):
        period = services.seed_demo(missing_store=missing_store, output_dir=output)
        imports = ", ".join(f"{b.location.code} ({b.row_count} rows)" for b in period.imports.all())
        self.stdout.write(self.style.SUCCESS(f"Seeded period {period}"))
        self.stdout.write(f"Time clock imports: {imports or 'none'}")
        if missing_store:
            self.stdout.write(self.style.WARNING(f"Store {missing_store.upper()} export left out on purpose"))
        self.stdout.write(f"Next: python manage.py run_payroll --period {period.start_date}")
