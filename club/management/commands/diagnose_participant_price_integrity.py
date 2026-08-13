import json

from django.core.management.base import BaseCommand

from club.participant_price_integrity_diagnostic import diagnose_participant_price_integrity


class Command(BaseCommand):
    help = "Read-only diagnosis of participant ticket price snapshot integrity"

    def handle(self, *args, **options):
        result = diagnose_participant_price_integrity()
        self.stdout.write(json.dumps(result, ensure_ascii=False, sort_keys=True))
