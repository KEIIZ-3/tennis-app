import json

from django.core.management.base import BaseCommand

from club.settlement_wallet_audit import audit_wallet_month


class Command(BaseCommand):
    help = "READ ONLY monthly company-wallet, ticket, court-cost, and settlement audit"

    def add_arguments(self, parser):
        parser.add_argument("year", type=int)
        parser.add_argument("month", type=int)

    def handle(self, *args, **options):
        result = audit_wallet_month(options["year"], options["month"])
        self.stdout.write(json.dumps(result, ensure_ascii=False, indent=2, default=str))
