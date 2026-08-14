import json

from django.core.management.base import BaseCommand

from club.settlement_carry_integrity_diagnostic import (
    diagnose_settlement_carry_integrity,
)


class Command(BaseCommand):
    help = "Read-only diagnosis of settlement payment, total, and carry integrity"

    def handle(self, *args, **options):
        result = diagnose_settlement_carry_integrity()
        self.stdout.write(json.dumps(result, ensure_ascii=False, sort_keys=True))
