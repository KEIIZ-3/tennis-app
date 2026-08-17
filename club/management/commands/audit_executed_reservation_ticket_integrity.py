import json

from django.core.management.base import BaseCommand

from club.executed_reservation_ticket_integrity_audit import audit_executed_reservation_ticket_integrity


class Command(BaseCommand):
    help = "READ ONLY Reservation-first audit of executed participant ticket integrity"

    def add_arguments(self, parser):
        parser.add_argument("year", type=int)
        parser.add_argument("month", type=int)
        parser.add_argument("--through-day", type=int, default=None)

    def handle(self, *args, **options):
        result = audit_executed_reservation_ticket_integrity(
            options["year"], options["month"], through_day=options["through_day"]
        )
        self.stdout.write(json.dumps(result, ensure_ascii=False, indent=2))
