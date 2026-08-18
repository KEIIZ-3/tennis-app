import json

from django.core.management.base import BaseCommand

from club.models import Reservation
from club.participant_price_snapshot_repair import (
    inspect_participant_price_snapshot_repair, repair_participant_price_snapshot,
)


class Command(BaseCommand):
    help = "保存済みConsumption証拠から欠落した参加者価格snapshotを補完します（既定dry-run）。"

    def add_arguments(self, parser):
        parser.add_argument("--reservation-id", action="append", type=int)
        parser.add_argument("--apply", action="store_true")

    def handle(self, *args, **options):
        ids = options["reservation_id"] or list(
            Reservation.objects.filter(
                participant_ticket_price_snapshot__isnull=True,
                tickets_used__gt=0,
            ).order_by("id").values_list("id", flat=True)
        )
        rows = []
        for reservation_id in ids:
            preview = inspect_participant_price_snapshot_repair(reservation_id)
            result = (
                repair_participant_price_snapshot(reservation_id)
                if options["apply"] and preview.candidate else preview
            )
            rows.append(result.to_dict())
        self.stdout.write(json.dumps({"dry_run": not options["apply"], "rows": rows}, ensure_ascii=False, indent=2))
