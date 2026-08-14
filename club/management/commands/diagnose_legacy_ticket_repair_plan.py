import json

from django.core.management.base import BaseCommand

from club.legacy_ticket_repair_plan import diagnose_legacy_ticket_repair_plan


class Command(BaseCommand):
    help = "永続証拠だけでlegacy ticketの補正可否をREAD ONLY分類します。"

    def handle(self, *args, **options):
        self.stdout.write(json.dumps(diagnose_legacy_ticket_repair_plan(), ensure_ascii=False, sort_keys=True, indent=2))
