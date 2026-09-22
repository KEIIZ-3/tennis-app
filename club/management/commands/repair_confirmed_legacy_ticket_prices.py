import json

from django.core.management.base import BaseCommand, CommandError

from club.confirmed_legacy_ticket_price_repair import (
    ConfirmedLegacyRepairRejected,
    apply_confirmed_legacy_prices,
    inspect_confirmed_legacy_prices,
    summarize_confirmed_rows,
)


class Command(BaseCommand):
    help = "Repair only the twelve user-confirmed legacy ticket prices (dry-run by default)."

    def add_arguments(self, parser):
        parser.add_argument("--reservation-id", action="append", type=int)
        parser.add_argument("--apply", action="store_true")

    def handle(self, *args, **options):
        if options["apply"] and options["reservation_id"]:
            raise CommandError("--apply always processes the complete confirmed set; omit --reservation-id")
        try:
            rows = inspect_confirmed_legacy_prices(reservation_ids=options["reservation_id"])
        except ConfirmedLegacyRepairRejected as exc:
            raise CommandError(str(exc)) from exc
        payload = {
            "dry_run": not options["apply"],
            "summary": summarize_confirmed_rows(rows),
            "rows": [row.to_dict() for row in rows],
        }
        self.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2))
        if options["apply"]:
            try:
                apply_confirmed_legacy_prices()
            except ConfirmedLegacyRepairRejected as exc:
                raise CommandError(str(exc)) from exc
            self.stdout.write(self.style.SUCCESS("confirmed legacy repair applied atomically"))
