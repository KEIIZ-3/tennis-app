import json

from django.core.management.base import BaseCommand

from club.ticket_integrity_diagnostic import diagnose_ticket_integrity


class Command(BaseCommand):
    help = "Read-only diagnosis of ticket balance and reservation integrity"

    def handle(self, *args, **options):
        self.stdout.write(json.dumps(diagnose_ticket_integrity(), ensure_ascii=False, sort_keys=True))
