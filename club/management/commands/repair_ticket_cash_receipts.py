import csv
from datetime import datetime

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from club.models import TicketCashReceipt, TicketPurchase, User, ensure_accounting_month_is_open
from club.ticket_cash_receipt_service import record_ticket_cash_receipt


class Command(BaseCommand):
    help = "Preview or apply explicitly confirmed historical ticket cash receipts from CSV"

    def add_arguments(self, parser):
        parser.add_argument("csv_path")
        parser.add_argument("--created-by", type=int, required=True)
        parser.add_argument("--apply", action="store_true")

    def handle(self, *args, **options):
        try:
            actor = User.objects.get(pk=options["created_by"])
        except User.DoesNotExist as exc:
            raise CommandError("The --created-by user does not exist.") from exc
        try:
            with open(options["csv_path"], encoding="utf-8-sig", newline="") as source:
                rows = [self._validate_row(raw, number) for number, raw in enumerate(csv.DictReader(source), 2)]
        except OSError as exc:
            raise CommandError(str(exc)) from exc
        if not rows:
            raise CommandError("The CSV contains no data rows.")
        purchase_ids = [row["purchase"].pk for row in rows]
        if len(purchase_ids) != len(set(purchase_ids)):
            raise CommandError("The CSV contains a duplicate ticket_purchase_id.")

        mode = "APPLY" if options["apply"] else "PREVIEW"
        for row in rows:
            purchase = row["purchase"]
            self.stdout.write(
                f'{mode} purchase_id={purchase.pk} user={purchase.user} amount={row["amount"]} '
                f'purchased_at={purchase.purchased_at.isoformat()} specified_received_at={row["received_at"].isoformat()} '
                f'existing_receipt={"yes" if row["existing_receipt"] else "no"} '
                f'can_apply={"yes" if row["can_apply"] else "no"} reason={row["reason"] or "-"}'
            )
        if not options["apply"]:
            self.stdout.write(self.style.WARNING("No changes made. Pass --apply to write eligible rows."))
            return

        with transaction.atomic():
            for row in rows:
                if row["can_apply"]:
                    record_ticket_cash_receipt(
                        ticket_purchase=row["purchase"], amount=row["amount"], received_at=row["received_at"],
                        payment_method=row["payment_method"], created_by=actor,
                        idempotency_key=f'cash-repair:{row["purchase"].pk}',
                    )
        self.stdout.write(self.style.SUCCESS(f'Applied {sum(row["can_apply"] for row in rows)} cash receipt(s).'))

    def _validate_row(self, raw, line_number):
        required = {"ticket_purchase_id", "amount", "received_at", "payment_method"}
        if not required.issubset(raw):
            raise CommandError(f"CSV headers must include: {', '.join(sorted(required))}")
        try:
            purchase = TicketPurchase.objects.select_related("user").get(pk=int(raw["ticket_purchase_id"]))
            amount = int(raw["amount"])
            received_at = datetime.fromisoformat(raw["received_at"])
            if timezone.is_naive(received_at):
                received_at = timezone.make_aware(received_at)
        except (ValueError, TicketPurchase.DoesNotExist) as exc:
            raise CommandError(f"Invalid row {line_number}: {exc}") from exc
        payment_method = raw["payment_method"].strip()
        if amount <= 0 or payment_method not in dict(TicketCashReceipt.PAYMENT_METHOD_CHOICES):
            raise CommandError(f"Invalid amount or payment_method on row {line_number}.")

        existing_receipt = TicketCashReceipt.objects.filter(ticket_purchase=purchase, reversed_at__isnull=True).first()
        reason = ""
        if existing_receipt:
            reason = "active receipt already exists"
        elif purchase.purchase_type in (TicketPurchase.PURCHASE_TYPE_FORMAL_FREE, TicketPurchase.PURCHASE_TYPE_LEGACY) or purchase.unit_price <= 0:
            reason = "purchase is free or legacy"
        else:
            try:
                ensure_accounting_month_is_open(received_at)
            except ValidationError as exc:
                reason = "; ".join(exc.messages)
        return {
            "purchase": purchase, "amount": amount, "received_at": received_at,
            "payment_method": payment_method, "existing_receipt": existing_receipt,
            "can_apply": not reason, "reason": reason,
        }
