import json

from django.core.management.base import BaseCommand, CommandError

from club.missing_ticket_consumption_repair import (
    MissingConsumptionRepairRejected,
    inspect_missing_ticket_consumptions,
    repair_missing_ticket_consumptions,
)


class Command(BaseCommand):
    help = "Ledger済みでConsumptionが不足する予約を診断し、安全なFIFOを復元します（既定preview）。"

    def add_arguments(self, parser):
        parser.add_argument("--user-id", action="append", type=int)
        parser.add_argument("--apply", action="store_true")

    def handle(self, *args, **options):
        rows = inspect_missing_ticket_consumptions(user_ids=options["user_id"])
        for row in rows:
            self.stdout.write(json.dumps(row.to_dict(), ensure_ascii=False, sort_keys=True))
        if not options["apply"]:
            return
        rejected = [row for row in rows if not row.candidate]
        if rejected:
            raise CommandError("rejected candidates exist; no changes applied")
        for user_id in sorted({row.user_id for row in rows}):
            try:
                repair_missing_ticket_consumptions(user_id=user_id)
            except MissingConsumptionRepairRejected as exc:
                raise CommandError(str(exc)) from exc
        self.stdout.write(self.style.SUCCESS(f"repaired users={len({row.user_id for row in rows})}"))
