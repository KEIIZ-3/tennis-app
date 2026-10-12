import json
from collections import Counter

from django.core.management.base import BaseCommand

from club.models import CoachAvailability
from club.prepaid_court_transfer_repair import (
    inspect_prepaid_court_transfer,
    repair_prepaid_court_transfer,
)


class Command(BaseCommand):
    help = "Preview or repair legacy prepaid court information without a canonical transfer."

    def add_arguments(self, parser):
        parser.add_argument("--availability-id", action="append", type=int)
        parser.add_argument("--apply", action="store_true")

    def handle(self, *args, **options):
        queryset = CoachAvailability.objects.select_related(
            "coach", "substitute_coach", "court", "court_payer_coach"
        ).order_by("id")
        availability_ids = options.get("availability_id") or []
        if availability_ids:
            queryset = queryset.filter(pk__in=availability_ids)

        rows = []
        errors = []
        for availability in queryset:
            try:
                preview = inspect_prepaid_court_transfer(availability)
                result = (
                    repair_prepaid_court_transfer(availability.pk)
                    if options["apply"] and preview.status == "candidate"
                    else preview
                )
                rows.append(result.to_dict())
            except Exception as exc:
                errors.append({"availability_id": availability.pk, "error": str(exc)})

        counts = Counter(row["status"] for row in rows)
        payload = {
            "dry_run": not options["apply"],
            "target_count": len(rows),
            "created": counts["created"],
            "candidates": counts["candidate"],
            "skipped_existing": counts["skipped_existing"],
            "skipped_closed_month": counts["skipped_closed_month"],
            "skipped_missing_payer": counts["skipped_missing_payer"],
            "skipped_unresolved_amount": counts["skipped_unresolved_amount"],
            "errors": errors,
            "rows": rows,
        }
        self.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2))
