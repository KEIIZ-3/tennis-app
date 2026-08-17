import json

from django.core.exceptions import ObjectDoesNotExist
from django.core.management.base import BaseCommand, CommandError

from club.legacy_ticket_consumption_repair import (
    RepairRejected,
    inspect_legacy_ticket_consumption_repair,
    repair_legacy_ticket_consumption,
)


class Command(BaseCommand):
    help = "明示した履歴予約の欠落TicketConsumptionだけを復元します（既定dry-run）。"

    def add_arguments(self, parser):
        parser.add_argument("--reservation-id", action="append", type=int, required=True)
        parser.add_argument("--apply", action="store_true", help="検証済みlinkageを実際に作成")

    def handle(self, *args, **options):
        reservation_ids = options["reservation_id"]
        rows = []
        failures = []
        previews = []
        for reservation_id in reservation_ids:
            try:
                preview = inspect_legacy_ticket_consumption_repair(reservation_id)
                previews.append(preview)
                if preview.status == "rejected":
                    failures.append(f"{reservation_id}:{preview.reason}")
            except (RepairRejected, ObjectDoesNotExist) as exc:
                failures.append(f"{reservation_id}:{exc}")
        if options["apply"] and failures:
            raise CommandError("repair rejected before apply: " + ", ".join(failures))
        for preview in previews:
            row = (
                repair_legacy_ticket_consumption(preview.reservation_id)
                if options["apply"]
                else preview
            )
            rows.append(row.to_dict())
        self.stdout.write(json.dumps({"dry_run": not options["apply"], "rows": rows}, ensure_ascii=False, indent=2))
        if failures:
            raise CommandError("repair rejected: " + ", ".join(failures))
