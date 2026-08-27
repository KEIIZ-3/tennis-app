import calendar
import uuid
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db import models
from django.db.models import Sum
from django.urls import reverse
from django.utils import timezone

from .models import (
    ShopInquiry, ShopPurchase, ShopQuote, ShopQuoteItem,
    ShopQuoteRevenueAllocation, ShopQuoteRevenueAllocationAudit,
    ShopRevenueAllocation, ShopRevenueAllocationAudit, User,
)
from .notification_service import freeze_recipients, schedule_delivery


def sale_price_from_discount(list_price, discount_rate):
    price = Decimal(str(list_price)) * (Decimal("1") - Decimal(str(discount_rate)) / Decimal("100"))
    return int(price.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def discount_rate_from_prices(list_price, sale_price):
    if not list_price:
        return None
    return ((Decimal(str(list_price)) - Decimal(str(sale_price))) * Decimal("100") / Decimal(str(list_price))).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)


def profit_summary(items):
    rows = list(items)
    revenue = sum(int(item.sale_price) * int(item.quantity) for item in rows)
    if any(item.cost_price is None for item in rows):
        return {"revenue": revenue, "cost": None, "profit": None, "margin": None}
    cost = sum(int(item.cost_price) * int(item.quantity) for item in rows)
    profit = revenue - cost
    margin = (Decimal(profit) * 100 / Decimal(revenue)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP) if revenue else None
    return {"revenue": revenue, "cost": cost, "profit": profit, "margin": margin}


def _schedule_inquiry_admin_notification(inquiry):
    admins = User.objects.filter(is_active=True).filter(models.Q(is_staff=True) | models.Q(is_superuser=True))
    message = "\n".join([
        "Shop価格問い合わせが届きました。", f"顧客名: {inquiry.customer.display_name()}",
        f"欲しいもの: {inquiry.wanted_item}",
        f"問い合わせ日時: {timezone.localtime(inquiry.created_at):%Y/%m/%d %H:%M}",
        f"確認画面: {reverse('club:shop_coach')}",
    ])
    schedule_delivery(freeze_recipients(admins), subject="Shop価格問い合わせ", message=message, media=("line", "email"), email_fallback=True)


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
    _schedule_inquiry_admin_notification(inquiry)
    return inquiry


@transaction.atomic
def create_quote(*, customer, creator, items, inquiry=None, note="", allocation_amounts=None):
    quote_date = timezone.localdate()
    quote = ShopQuote.objects.create(
        quote_number=f"PENDING-{uuid.uuid4().hex[:12]}", customer=customer,
        inquiry=inquiry, quote_date=quote_date, valid_until=one_month_after(quote_date),
        note=(note or "").strip(), created_by=creator,
    )
    quote.quote_number = quote_number_for(quote)
    quote.save(update_fields=["quote_number"])
    for order, data in enumerate(items):
        item_data = {key: data.get(key) for key in ("description", "quantity", "list_price", "sale_price", "cost_price")}
        item = ShopQuoteItem(quote=quote, sort_order=order, **item_data)
        item.full_clean()
        item.save()
    if not quote.items.exists():
        raise ValidationError("見積明細を1件以上入力してください。")
    if inquiry:
        inquiry.status = ShopInquiry.STATUS_QUOTED
        inquiry.quoted_amount = quote.total
        inquiry.assigned_coach = creator if creator.role in User.COACH_ROLE_VALUES else inquiry.assigned_coach
        inquiry.save(update_fields=["status", "quoted_amount", "assigned_coach", "updated_at"])
    if allocation_amounts is not None:
        save_quote_allocations(quote=quote, actor=creator, amounts=allocation_amounts)
    return quote


@transaction.atomic
def request_purchase(*, quote, customer):
    quote = ShopQuote.objects.select_for_update().get(pk=quote.pk, customer=customer)
    if quote.status != ShopQuote.STATUS_SENT:
        raise ValidationError("購入希望を送信できる見積ではありません。")
    quote.status = ShopQuote.STATUS_PURCHASE_REQUESTED
    quote.save(update_fields=["status", "updated_at"])
    if quote.inquiry_id:
        ShopInquiry.objects.filter(pk=quote.inquiry_id).update(status=ShopInquiry.STATUS_PURCHASE_REQUESTED)
    return quote


@transaction.atomic
def update_quote(*, quote, customer, items, note="", actor=None, allocation_amounts=None):
    quote = ShopQuote.objects.select_for_update().prefetch_related("items").get(pk=quote.pk)
    if quote.status in (ShopQuote.STATUS_PURCHASED, ShopQuote.STATUS_CANCELED) or hasattr(quote, "purchase"):
        raise ValidationError("購入確定済みまたは取消済みの見積は編集できません。")
    previous_allocations = {row.coach_id: row.amount for row in quote.planned_allocations.all()}
    rows = []
    for order, data in enumerate(items):
        item_data = {key: data.get(key) for key in (
            "description", "quantity", "list_price", "sale_price", "cost_price",
        )}
        item = ShopQuoteItem(quote=quote, sort_order=order, **item_data)
        item.full_clean()
        rows.append(item)
    if not rows:
        raise ValidationError("見積明細を1件以上入力してください。")
    quote.customer = customer
    quote.note = (note or "").strip()
    if quote.status == ShopQuote.STATUS_PURCHASE_REQUESTED:
        quote.status = ShopQuote.STATUS_SENT
    quote.save(update_fields=["customer", "note", "status", "updated_at"])
    quote.items.all().delete()
    ShopQuoteItem.objects.bulk_create(rows)
    quote._prefetched_objects_cache.pop("items", None)
    if quote.inquiry_id:
        ShopInquiry.objects.filter(pk=quote.inquiry_id).update(
            status=ShopInquiry.STATUS_QUOTED, quoted_amount=quote.total,
        )
    normalized_input = ({int(key): int(value or 0) for key, value in allocation_amounts.items()}
                        if allocation_amounts is not None else None)
    if normalized_input is not None and not (
            normalized_input == previous_allocations and sum(normalized_input.values()) > quote.total):
        save_quote_allocations(quote=quote, actor=actor, amounts=allocation_amounts)
    return quote


@transaction.atomic
def confirm_quote_purchase(*, quote, actor):
    quote = ShopQuote.objects.select_for_update().prefetch_related("items").get(pk=quote.pk)
    existing = ShopPurchase.objects.filter(quote=quote).first()
    if existing:
        return existing, False
    if quote.status not in (ShopQuote.STATUS_SENT, ShopQuote.STATUS_PURCHASE_REQUESTED):
        raise ValidationError("購入確定前の有効な見積のみ購入確定できます。")
    purchase, created = ShopPurchase.objects.get_or_create(
        quote=quote,
        defaults={"customer": quote.customer, "description": "\n".join(i.description for i in quote.items.all()),
                  "quantity": sum(i.quantity for i in quote.items.all()), "amount": quote.total,
                  "cost_total": profit_summary(quote.items.all())["cost"],
                  "note": quote.note, "registered_by": actor},
    )
    if created:
        purchase.full_clean()
        planned = {row.coach_id: row.amount for row in quote.planned_allocations.all()}
        applied = False
        if planned and sum(planned.values()) <= purchase.amount:
            try:
                _save_purchase_allocations(purchase=purchase, actor=actor, amounts=planned,
                                           require_admin=False)
                applied = True
            except ValidationError:
                # A coach may have become inactive after the plan was saved. Purchase remains valid.
                pass
        if planned and not applied:
            ShopRevenueAllocationAudit.objects.create(
                purchase=purchase,
                allocation_snapshot=[
                    {"coach_id": coach_id, "amount": amount, "not_applied": True}
                    for coach_id, amount in planned.items()
                ],
                changed_by=actor,
            )
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
def _normalize_allocations(*, amounts, maximum):
    try:
        normalized = {int(coach_id): int(amount or 0) for coach_id, amount in amounts.items()}
    except (TypeError, ValueError):
        raise ValidationError("按分額は0以上の整数で入力してください。")
    if any(amount < 0 for amount in normalized.values()):
        raise ValidationError("按分額は0円以上にしてください。")
    if sum(normalized.values()) > maximum:
        raise ValidationError("按分合計がShop売上を超えています。")
    coaches = {u.pk: u for u in User.objects.filter(
        pk__in=normalized, role__in=User.COACH_ROLE_VALUES, is_active=True,
    )}
    if set(normalized) != set(coaches):
        raise ValidationError("按分対象にできないユーザーが含まれています。")
    return normalized, coaches


@transaction.atomic
def save_quote_allocations(*, quote, actor, amounts):
    if not actor or not (actor.is_staff or actor.is_superuser):
        raise PermissionError("adminのみ見積時の売上按分を変更できます。")
    quote = ShopQuote.objects.select_for_update().get(pk=quote.pk)
    if quote.status in (ShopQuote.STATUS_PURCHASED, ShopQuote.STATUS_CANCELED) or hasattr(quote, "purchase"):
        raise ValidationError("購入確定済みまたは取消済みの見積按分は変更できません。")
    normalized, coaches = _normalize_allocations(amounts=amounts, maximum=quote.total)
    snapshot = []
    for coach_id, amount in normalized.items():
        ShopQuoteRevenueAllocation.objects.update_or_create(
            quote=quote, coach=coaches[coach_id], defaults={"amount": amount, "created_by": actor},
        )
        snapshot.append({"coach_id": coach_id, "amount": amount})
    ShopQuoteRevenueAllocationAudit.objects.create(
        quote=quote, allocation_snapshot=snapshot, changed_by=actor,
    )
    return quote_allocation_summary(quote)


def quote_allocation_summary(quote):
    allocated = quote.planned_allocations.aggregate(total=Sum("amount"))["total"] or 0
    remaining = int(quote.total) - int(allocated)
    return {"allocated": allocated, "remaining": remaining, "complete": remaining == 0}


def _save_purchase_allocations(*, purchase, actor, amounts, require_admin):
    if require_admin and not (actor.is_staff or actor.is_superuser):
        raise PermissionError("adminのみ売上按分を変更できます。")
    purchase = ShopPurchase.objects.select_for_update().get(pk=purchase.pk)
    if purchase.status != ShopPurchase.STATUS_CONFIRMED:
        raise ValidationError("購入確定済みのShop売上だけ按分できます。")
    normalized, coaches = _normalize_allocations(amounts=amounts, maximum=purchase.amount)
    snapshot = []
    for coach_id, amount in normalized.items():
        allocation, _ = ShopRevenueAllocation.objects.update_or_create(
            purchase=purchase, coach=coaches[coach_id], defaults={"amount": amount, "created_by": actor})
        snapshot.append({"coach_id": coach_id, "amount": amount})
    ShopRevenueAllocationAudit.objects.create(purchase=purchase, allocation_snapshot=snapshot, changed_by=actor)
    return allocation_summary(purchase)


@transaction.atomic
def save_allocations(*, purchase, actor, amounts):
    return _save_purchase_allocations(
        purchase=purchase, actor=actor, amounts=amounts, require_admin=True,
    )


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
