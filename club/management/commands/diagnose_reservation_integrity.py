import json

from django.core.management.base import BaseCommand
from django.db import connection, transaction

from club.reservation_integrity_diagnostic import diagnose_reservation_integrity


class Command(BaseCommand):
    help = "READ ONLY occurrence-level Reservation source-of-truth audit"

    def handle(self, *args, **options):
        with transaction.atomic():
            if connection.vendor == "postgresql":
                with connection.cursor() as cursor:
                    cursor.execute("SET TRANSACTION READ ONLY")
            result = diagnose_reservation_integrity()
        self.stdout.write(json.dumps(result, ensure_ascii=False, sort_keys=True))
