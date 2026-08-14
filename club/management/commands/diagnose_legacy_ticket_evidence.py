import json

from django.core.management.base import BaseCommand

from club.legacy_ticket_evidence_diagnostic import diagnose_legacy_ticket_evidence


class Command(BaseCommand):
    help = "Read-only classification of persisted legacy ticket evidence"

    def handle(self, *args, **options):
        self.stdout.write(json.dumps(diagnose_legacy_ticket_evidence(), ensure_ascii=False, sort_keys=True))
