from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError

from core import services


class Command(BaseCommand):
    help = "Delete visitor sandboxes not seen for a while (their runs and decisions go with them)."

    def add_arguments(self, parser):
        parser.add_argument("--older-than-hours", type=float, default=24,
                            help="Delete sandboxes whose last activity is older than this (default 24)")

    def handle(self, *args, older_than_hours, **options):
        if older_than_hours < 0:
            raise CommandError("--older-than-hours must be zero or more")
        removed = services.cleanup_sandboxes(timedelta(hours=older_than_hours))
        self.stdout.write(self.style.SUCCESS(
            f"Removed {removed} sandbox{'es' if removed != 1 else ''} older than {older_than_hours:g} hours"))
