import json

from django.core.management.base import BaseCommand

from club.participant_price_backfill_impact import diagnose_participant_price_backfill_impact


class Command(BaseCommand):
    help = "Read-only diagnosis of recoverable participant price backfill impact"

    def handle(self, *args, **options):
        result = diagnose_participant_price_backfill_impact()
        self.stdout.write(json.dumps(result, ensure_ascii=False, sort_keys=True))
