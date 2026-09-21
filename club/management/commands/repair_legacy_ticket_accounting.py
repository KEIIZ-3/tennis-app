import json
from datetime import date

from django.core.management.base import BaseCommand, CommandError

from club.legacy_ticket_accounting_repair import (
    DEFAULT_FROM_DATE,
    apply_legacy_ticket_accounting,
    inspect_legacy_ticket_accounting,
)


class Command(BaseCommand):
    help = "Reconstruct legacy reservation ticket accounting by persisted FIFO history (dry-run by default)."

    def add_arguments(self, parser):
        parser.add_argument("--from-date", type=date.fromisoformat, default=DEFAULT_FROM_DATE)
        parser.add_argument("--to-date", type=date.fromisoformat)
        parser.add_argument("--reservation-id", action="append", type=int)
        parser.add_argument("--apply", action="store_true")

    def handle(self, *args, **options):
        if options["to_date"] and options["to_date"] < options["from_date"]:
            raise CommandError("--to-date must not be earlier than --from-date")
        kwargs = {
            "from_date": options["from_date"],
            "to_date": options["to_date"],
            "reservation_ids": options["reservation_id"],
        }
        rows = inspect_legacy_ticket_accounting(**kwargs)
        payload = {
            "dry_run": not options["apply"],
            "summary": {
                status: sum(row.repair_status == status for row in rows)
                for status in ("repairable", "ambiguous", "already_ok", "skipped_closed_month", "excluded_test_period")
            },
            "rows": [row.to_dict() for row in rows],
        }
        self.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2))
        if options["apply"]:
            repaired = apply_legacy_ticket_accounting(**kwargs)
            self.stdout.write(self.style.SUCCESS(f"repaired reservations={len(repaired)}"))
