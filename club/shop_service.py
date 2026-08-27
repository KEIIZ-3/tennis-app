import calendar
import uuid
from datetime import date

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Sum
from django.utils import timezone

from .models import (
    ShopInquiry, ShopPurchase, ShopQuote, ShopQuoteItem,
    ShopRevenueAllocation, ShopRevenueAllocationAudit, User,
)


def one_month_after(value):
    year, month = value.year, value.month + 1
    if month == 13:
        year, month = year + 1, 1
    return date(year, month, min(value.day, calendar.monthrange(year, month)[1]))


def quote_number_for(quote):
    return f"EST-{quote.quote_date:%Y%m}-{quote.pk:04d}"


@transaction.atomic
def create_inquiry(*, customer, wanted_item):
    inquiry = ShopInquiry(customer=customer, wanted_item=wanted_item)
    inquiry.full_clean()
    inquiry.save()
    return inquiry


@transaction.atomic
def create_quote(*, customer, creator, items, inquiry=None, note=""):
    quote_date = timezone.localdate()
    quote = ShopQuote.objects.create(
        quote_number=f"PENDING-{uuid.uuid4().hex[:12]}", customer=customer,
        inquiry=inquiry, quote_date=quote_date, valid_until=one_month_after(quote_date),
        note=(note or "").strip(), created_by=creator,
    )
    quote.quote_number = quote_number_for(quote)
    quote.save(update_fields=["quote_number"])
    for order, data in enumerate(items):
        item = ShopQuoteItem(quote=quote, sort_order=order, **data)
        item.full_clean()
        item.save()
    if not quote.items.exists():
        raise ValidationError("見積明細を1件以上入力してください。")
    if inquiry:
        inquiry.status = ShopInquiry.STATUS_QUOTED
        inquiry.quoted_amount = quote.total
        inquiry.assigned_coach = creator if creator.role in User.COACH_ROLE_VALUES else inquiry.assigned_coach
        inquiry.save(update_fields=["status", "quoted_amount", "assigned_coach", "updated_at"])
    return quote


@transaction.atomic
def request_purchase(*, quote, customer):
    quote = ShopQuote.objects.select_for_update().get(pk=quote.pk, customer=customer)
    if quote.status == ShopQuote.STATUS_CANCELED:
        raise ValidationError("取消済みの見積です。")
    if quote.status != ShopQuote.STATUS_PURCHASED:
        quote.status = ShopQuote.STATUS_PURCHASE_REQUESTED
        quote.save(update_fields=["status", "updated_at"])
        if quote.inquiry_id:
            ShopInquiry.objects.filter(pk=quote.inquiry_id).update(status=ShopInquiry.STATUS_PURCHASE_REQUESTED)
    return quote


@transaction.atomic
def confirm_quote_purchase(*, quote, actor):
    quote = ShopQuote.objects.select_for_update().prefetch_related("items").get(pk=quote.pk)
    purchase, created = ShopPurchase.objects.get_or_create(
        quote=quote,
        defaults={"customer": quote.customer, "description": "\n".join(i.description for i in quote.items.all()),
                  "quantity": sum(i.quantity for i in quote.items.all()), "amount": quote.total,
                  "note": quote.note, "registered_by": actor},
    )
    if created:
        purchase.full_clean()
        quote.status = ShopQuote.STATUS_PURCHASED
        quote.save(update_fields=["status", "updated_at"])
        if quote.inquiry_id:
            ShopInquiry.objects.filter(pk=quote.inquiry_id).update(status=ShopInquiry.STATUS_PURCHASED, purchased_at=purchase.purchased_at)
    return purchase, created


@transaction.atomic
def create_direct_purchase(*, customer, actor, description, quantity, amount, note=""):
    purchase = ShopPurchase(customer=customer, registered_by=actor, description=description,
                            quantity=quantity, amount=amount, note=note)
    purchase.full_clean()
    purchase.save()
    return purchase


@transaction.atomic
def save_allocations(*, purchase, actor, amounts):
    if not (actor.is_staff or actor.is_superuser):
        raise PermissionError("adminのみ売上按分を変更できます。")
    purchase = ShopPurchase.objects.select_for_update().get(pk=purchase.pk)
    if purchase.status != ShopPurchase.STATUS_CONFIRMED:
        raise ValidationError("購入確定済みのShop売上だけ按分できます。")
    normalized = {int(coach_id): int(amount or 0) for coach_id, amount in amounts.items()}
    if any(amount < 0 for amount in normalized.values()):
        raise ValidationError("按分額は0円以上にしてください。")
    if sum(normalized.values()) > purchase.amount:
        raise ValidationError("按分合計がShop売上を超えています。")
    coaches = {u.pk: u for u in User.objects.filter(pk__in=normalized, role__in=User.COACH_ROLE_VALUES, is_active=True)}
    if set(normalized) != set(coaches):
        raise ValidationError("按分対象にできないユーザーが含まれています。")
    snapshot = []
    for coach_id, amount in normalized.items():
        allocation, _ = ShopRevenueAllocation.objects.update_or_create(
            purchase=purchase, coach=coaches[coach_id], defaults={"amount": amount, "created_by": actor})
        snapshot.append({"coach_id": coach_id, "amount": amount})
    ShopRevenueAllocationAudit.objects.create(purchase=purchase, allocation_snapshot=snapshot, changed_by=actor)
    return allocation_summary(purchase)


def allocation_summary(purchase):
    allocated = purchase.allocations.aggregate(total=Sum("amount"))["total"] or 0
    remaining = int(purchase.amount) - int(allocated)
    return {"allocated": allocated, "remaining": remaining, "complete": purchase.status == ShopPurchase.STATUS_CONFIRMED and remaining == 0}


def monthly_shop_allocations(year, month):
    rows = (ShopRevenueAllocation.objects.filter(
        purchase__status=ShopPurchase.STATUS_CONFIRMED,
        purchase__purchased_at__year=year, purchase__purchased_at__month=month,
    ).values("coach_id").annotate(total=Sum("amount")))
    return {row["coach_id"]: int(row["total"] or 0) for row in rows}
