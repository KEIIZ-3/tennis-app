import json
from datetime import datetime

from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from django.utils.dateparse import parse_datetime
from django.utils.timezone import is_naive

from club.ticket_lifecycle_audit import LEGACY_BASELINE, diagnose_ticket_lifecycle


class Command(BaseCommand):
    help = "Phase 1 ticket lifecycleをPostgreSQL READ ONLY transactionで監査します。"

    def add_arguments(self, parser):
        parser.add_argument("--baseline", default=LEGACY_BASELINE)
        parser.add_argument("--pr209-deployed-at", help="ISO-8601 timestamp; omitted when the boundary is not proven")

    def handle(self, *args, **options):
        deployed_at = None
        if options["pr209_deployed_at"]:
            deployed_at = parse_datetime(options["pr209_deployed_at"])
            if deployed_at is None or is_naive(deployed_at):
                raise CommandError("--pr209-deployed-at must be an ISO-8601 timestamp with timezone")
        with transaction.atomic():
            if connection.vendor == "postgresql":
                with connection.cursor() as cursor:
                    cursor.execute("SET TRANSACTION READ ONLY")
            result = diagnose_ticket_lifecycle(pr209_deployed_at=deployed_at, baseline=options["baseline"])
        self.stdout.write(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2, default=_json_default))


def _json_default(value):
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")
