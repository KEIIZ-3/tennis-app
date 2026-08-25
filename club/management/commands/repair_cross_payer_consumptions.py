import json

from django.core.exceptions import ObjectDoesNotExist
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from club.cross_payer_consumption_repair import (
    CrossPayerRepairRejected,
    inspect_cross_payer_consumption_repair,
    repair_cross_payer_consumption,
)


class Command(BaseCommand):
    help = "Preview or apply formal cross-payer pending-consumption linkage repair."

    def add_arguments(self, parser):
        parser.add_argument("--reservation-id", action="append", type=int, required=True)
        parser.add_argument("--apply", action="store_true")

    def handle(self, *args, **options):
        previews = []
        failures = []
        for reservation_id in options["reservation_id"]:
            try:
                preview = inspect_cross_payer_consumption_repair(reservation_id)
                previews.append(preview)
                if preview.status == "rejected":
                    failures.append(f"{reservation_id}:{preview.reason}")
            except ObjectDoesNotExist as exc:
                failures.append(f"{reservation_id}:{exc}")
        if failures and options["apply"]:
            raise CommandError("repair rejected before apply: " + ", ".join(failures))

        rows = []
        try:
            with transaction.atomic():
                for preview in previews:
                    result = (
                        repair_cross_payer_consumption(preview.reservation_id)
                        if options["apply"] else preview
                    )
                    rows.append(result.to_dict())
        except CrossPayerRepairRejected as exc:
            raise CommandError(f"repair rejected during apply: {exc}") from exc
        self.stdout.write(json.dumps({"dry_run": not options["apply"], "rows": rows}, ensure_ascii=False, indent=2))
        if failures:
            raise CommandError("repair rejected: " + ", ".join(failures))
