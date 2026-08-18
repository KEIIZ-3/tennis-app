import json

from django.core.exceptions import ObjectDoesNotExist
from django.core.management.base import BaseCommand, CommandError

from club.historical_ticket_consumption_repair import (
    CONFIRMED_PRICE_WITHOUT_PURCHASE,
    HistoricalRepairRejected,
    inspect_historical_ticket_consumption_repair,
    repair_historical_ticket_consumption,
)
from club.missing_ticket_purchase_evidence_audit import (
    DEFAULT_RESERVATION_IDS,
    audit_missing_ticket_purchase_evidence,
)

APPROVED_PURCHASE_EVIDENCE = {
    1506: (14, 500), 1523: (15, 3500), 1498: (20, 3500),
    1501: (20, 3500), 1493: (21, 3500), 1503: (16, 3500),
    1504: (16, 3500), 1526: (22, 3500), 1531: (19, 1000),
    1541: (23, 3500), 1552: (25, 3500), 1553: (24, 3500),
    1495: (26, 3500),
}


class Command(BaseCommand):
    help = "承認済み15件のhistorical TicketConsumption証拠を復元します（既定dry-run）。"

    def add_arguments(self, parser):
        parser.add_argument("--reservation-id", action="append", type=int)
        parser.add_argument("--apply", action="store_true")

    def handle(self, *args, **options):
        reservation_ids = options["reservation_id"] or list(DEFAULT_RESERVATION_IDS)
        unknown = sorted(set(reservation_ids) - set(DEFAULT_RESERVATION_IDS))
        if unknown:
            raise CommandError(f"not an approved historical reservation: {unknown}")

        audit = audit_missing_ticket_purchase_evidence(DEFAULT_RESERVATION_IDS)
        audited = {row["reservation_id"]: row for row in audit["rows"]}
        inputs = []
        failures = []
        for reservation_id in reservation_ids:
            row = audited.get(reservation_id)
            purchase_id = None if row is None else row["candidate_later_purchase_id"]
            confirmed_price = CONFIRMED_PRICE_WITHOUT_PURCHASE.get(reservation_id)
            if row is not None and reservation_id != 1525 and purchase_id is None:
                failures.append(f"{reservation_id}:fifo_candidate_not_confirmed")
                continue
            expected = APPROVED_PURCHASE_EVIDENCE.get(reservation_id)
            if row is not None and expected and (
                purchase_id, row["candidate_purchase_unit_price"]
            ) != expected:
                failures.append(f"{reservation_id}:approved_purchase_evidence_mismatch")
                continue
            inputs.append((reservation_id, purchase_id, confirmed_price))

        previews = []
        for reservation_id, purchase_id, confirmed_price in inputs:
            try:
                preview = inspect_historical_ticket_consumption_repair(
                    reservation_id,
                    candidate_purchase_id=purchase_id,
                    confirmed_unit_price=confirmed_price,
                )
                previews.append((preview, purchase_id, confirmed_price))
                if preview.status == "rejected":
                    failures.append(f"{reservation_id}:{preview.reason}")
            except (HistoricalRepairRejected, ObjectDoesNotExist) as exc:
                failures.append(f"{reservation_id}:{exc}")
        if options["apply"] and failures:
            raise CommandError("repair rejected before apply: " + ", ".join(failures))

        rows = []
        for preview, purchase_id, confirmed_price in previews:
            result = (
                repair_historical_ticket_consumption(
                    preview.reservation_id,
                    candidate_purchase_id=purchase_id,
                    confirmed_unit_price=confirmed_price,
                )
                if options["apply"] else preview
            )
            rows.append(result.to_dict())
        self.stdout.write(json.dumps({"dry_run": not options["apply"], "rows": rows}, ensure_ascii=False, indent=2))
        if failures:
            raise CommandError("repair rejected: " + ", ".join(failures))
