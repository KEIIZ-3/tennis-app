from __future__ import annotations

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from club.models import CoachAvailability

User = get_user_model()


class Command(BaseCommand):
    help = "指定コーチに向けて今後数日分のデモ空き枠を投入します。"

    def add_arguments(self, parser):
        parser.add_argument("--coach", required=True, help="coach username")
        parser.add_argument("--days", type=int, default=7, help="作成日数")
        parser.add_argument("--capacity", type=int, default=1, help="各枠の定員")

    def handle(self, *args, **options):
        username = options["coach"]
        days = int(options["days"])
        capacity = int(options["capacity"])

        try:
            coach = User.objects.get(username=username, role="coach")
        except User.DoesNotExist as exc:
            raise CommandError(f"coach not found: {username}") from exc

        today = timezone.localdate()
        created_count = 0

        for i in range(days):
            target_date = today + timedelta(days=i)
            for start_h, end_h in [(9, 10), (10, 11), (11, 12), (13, 14), (14, 15), (15, 16)]:
                _, created = CoachAvailability.objects.get_or_create(
                    coach=coach,
                    date=target_date,
                    start_time=f"{start_h:02d}:00",
                    end_time=f"{end_h:02d}:00",
                    defaults={
                        "capacity": capacity,
                        "status": "available",
                    },
                )
                if created:
                    created_count += 1

        self.stdout.write(self.style.SUCCESS(f"created availabilities: {created_count}"))
