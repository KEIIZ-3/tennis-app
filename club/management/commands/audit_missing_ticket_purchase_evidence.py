import json

from django.core.management.base import BaseCommand

from club.missing_ticket_purchase_evidence_audit import (
    DEFAULT_RESERVATION_IDS,
    audit_missing_ticket_purchase_evidence,
    pending_evidence_candidate_ids,
)


class Command(BaseCommand):
    help = "欠落TicketConsumptionのPurchase根拠をSELECT-onlyで監査します。"

    def add_arguments(self, parser):
        parser.add_argument("--reservation-id", action="append", type=int)
        parser.add_argument("--all-pending-evidence-candidates", action="store_true")

    def handle(self, *args, **options):
        reservation_ids = (
            pending_evidence_candidate_ids()
            if options["all_pending_evidence_candidates"]
            else options["reservation_id"] or DEFAULT_RESERVATION_IDS
        )
        self.stdout.write(json.dumps(audit_missing_ticket_purchase_evidence(reservation_ids), ensure_ascii=False, indent=2))
