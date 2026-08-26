import csv
from datetime import datetime

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from club.models import TicketCashReceipt, TicketPurchase, User
from club.ticket_cash_receipt_service import record_ticket_cash_receipt


class Command(BaseCommand):
    help = "CSVで明示された既存チケット購入の現金受領をpreview/applyする"

    def add_arguments(self, parser):
        parser.add_argument("csv_path")
        parser.add_argument("--created-by", type=int, required=True)
        parser.add_argument("--apply", action="store_true")

    def handle(self, *args, **options):
        try:
            actor = User.objects.get(pk=options["created_by"])
        except User.DoesNotExist as exc:
            raise CommandError("--created-by のユーザーが存在しません。") from exc

        rows = []
        try:
            with open(options["csv_path"], encoding="utf-8-sig", newline="") as source:
                for line_number, raw in enumerate(csv.DictReader(source), start=2):
                    rows.append(self._validate_row(raw, line_number))
        except OSError as exc:
            raise CommandError(str(exc)) from exc
        if not rows:
            raise CommandError("CSVにデータ行がありません。")

        mode = "APPLY" if options["apply"] else "PREVIEW"
        for row in rows:
            self.stdout.write(
                f'{mode} purchase_id={row["purchase"].pk} amount={row["amount"]} '
                f'received_at={row["received_at"].isoformat()} payment_method={row["payment_method"]}'
            )
        if not options["apply"]:
            self.stdout.write(self.style.WARNING("変更なし。適用するには --apply を指定してください。"))
            return

        with transaction.atomic():
            for row in rows:
                record_ticket_cash_receipt(
                    ticket_purchase=row["purchase"], amount=row["amount"],
                    received_at=row["received_at"], payment_method=row["payment_method"],
                    created_by=actor, idempotency_key=f'cash-repair:{row["purchase"].pk}',
                )
        self.stdout.write(self.style.SUCCESS(f"{len(rows)}件を適用しました。"))

    def _validate_row(self, raw, line_number):
        required = {"ticket_purchase_id", "amount", "received_at", "payment_method"}
        if not required.issubset(raw):
            raise CommandError(f"CSVヘッダーは {', '.join(sorted(required))} が必要です。")
        try:
            purchase = TicketPurchase.objects.get(pk=int(raw["ticket_purchase_id"]))
            amount = int(raw["amount"])
            received_at = datetime.fromisoformat(raw["received_at"])
            if timezone.is_naive(received_at):
                received_at = timezone.make_aware(received_at)
        except (ValueError, TicketPurchase.DoesNotExist) as exc:
            raise CommandError(f"{line_number}行目が不正です: {exc}") from exc
        payment_method = raw["payment_method"].strip()
        if amount <= 0 or payment_method not in dict(TicketCashReceipt.PAYMENT_METHOD_CHOICES):
            raise CommandError(f"{line_number}行目の金額または支払方法が不正です。")
        if TicketCashReceipt.objects.filter(idempotency_key=f"cash-repair:{purchase.pk}").exists():
            raise CommandError(f"{line_number}行目の購入は適用済みです。")
        return {"purchase": purchase, "amount": amount, "received_at": received_at, "payment_method": payment_method}
