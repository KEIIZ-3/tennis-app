from datetime import date, timedelta

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from club.court_payment_reminders import send_reminders


class Command(BaseCommand):
    help = "8日後の未払いコート代をメインコーチへLINEで通知します。"

    def add_arguments(self, parser):
        parser.add_argument("--target-date", help="対象日 (YYYY-MM-DD)")
        parser.add_argument("--dry-run", action="store_true", help="LINEを送信せず内容を表示")

    def handle(self, *args, **options):
        raw_date = options.get("target_date")
        try:
            target_date = date.fromisoformat(raw_date) if raw_date else timezone.localdate() + timedelta(days=8)
        except ValueError as exc:
            raise CommandError("--target-date は YYYY-MM-DD 形式で指定してください。") from exc

        availabilities, recipients, message, results = send_reminders(
            target_date, dry_run=options["dry_run"]
        )
        self.stdout.write(f"対象日: {target_date.isoformat()}")
        self.stdout.write("対象availability: " + (", ".join(str(row.pk) for row in availabilities) or "なし"))
        self.stdout.write("対象コーチ: " + (", ".join(user.display_name() for user in recipients) or "なし"))
        if message:
            self.stdout.write(message)
        for recipient, status in results:
            self.stdout.write(f"{recipient.display_name()}: {status}")
