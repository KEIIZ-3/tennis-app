import json

from django.core.management.base import BaseCommand

from club.ticket_consumption_audit import audit_ticket_consumptions


class Command(BaseCommand):
    help = "READ ONLY monthly TicketConsumption detail and summary audit"

    def add_arguments(self, parser):
        parser.add_argument("year", type=int)
        parser.add_argument("month", type=int)

    def handle(self, *args, **options):
        result = audit_ticket_consumptions(options["year"], options["month"])
        self.stdout.write(json.dumps(result, ensure_ascii=False, indent=2))
