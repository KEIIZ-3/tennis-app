import json

from django.core.management.base import BaseCommand

from club.court_rain_integrity_diagnostic import diagnose_court_rain_integrity


class Command(BaseCommand):
    help = "Read-only diagnosis of court transfer and rain refund integrity"

    def handle(self, *args, **options):
        result = diagnose_court_rain_integrity()
        self.stdout.write(json.dumps(result, ensure_ascii=False, sort_keys=True))
