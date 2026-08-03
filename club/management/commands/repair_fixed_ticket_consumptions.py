from django.core.management.base import BaseCommand

from club.fixed_ticket_consumption_repair import repair_missing_fixed_ticket_consumptions


class Command(BaseCommand):
    help = "固定予約で未実施のチケット消費を安全に補正します。"

    def add_arguments(self, parser):
        parser.add_argument(
            "--all-active",
            action="store_true",
            help="開始前を含む有効な固定予約も補正対象にします。",
        )

    def handle(self, *args, **options):
        result = repair_missing_fixed_ticket_consumptions(
            past_only=not bool(options.get("all_active")),
        )

        self.stdout.write(
            self.style.SUCCESS(
                f"補正完了: {result['repaired_count']}件"
            )
        )
        if result["repaired_ids"]:
            self.stdout.write(
                "補正予約ID: " + ", ".join(str(value) for value in result["repaired_ids"])
            )

        if result["skipped_count"]:
            self.stdout.write(f"スキップ: {result['skipped_count']}件")
            for reservation_id, reason in result["skipped"]:
                self.stdout.write(f"- 予約#{reservation_id}: {reason}")
