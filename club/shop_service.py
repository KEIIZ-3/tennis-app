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
    cost = sum(
        int(item.cost_price) * int(item.quantity)
        for item in rows if item.cost_price is not None
    )
    costs_complete = all(item.cost_price is not None for item in rows)
    if not costs_complete:
        return {
            "revenue": revenue, "cost": cost, "profit": None, "margin": None,
            "costs_complete": False,
        }
    profit = revenue - cost
    margin = (Decimal(profit) * 100 / Decimal(revenue)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP) if revenue else None
    return {
        "revenue": revenue, "cost": cost, "profit": profit, "margin": margin,
        "costs_complete": True,
    }


def can_manage_shop_accounting(user):
    return bool(user and user.is_authenticated and user.is_superuser)


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
def create_quote(*, customer, creator, items, inquiry=None, note="", accounting=None, guest_name=""):
    quote_date = timezone.localdate()
    quote = ShopQuote(
        quote_number=f"PENDING-{uuid.uuid4().hex[:12]}", customer=customer,
        guest_name=guest_name,
        inquiry=inquiry, quote_date=quote_date, valid_until=one_month_after(quote_date),
        note=(note or "").strip(), created_by=creator,
    )
    quote.full_clean()
    quote.save()
    quote.quote_number = quote_number_for(quote)
    quote.save(update_fields=["quote_number"])
    for order, data in enumerate(items):
        item_data = {key: data.get(key) for key in ("description", "quantity", "list_price", "sale_price", "cost_price")}
        item = ShopQuoteItem(quote=quote, sort_order=order, **item_data)
        item.full_clean()
        item.save()
    if not quote.items.exists():
        raise ValidationError("見積明細を1件以上入力してください。")
    if accounting is not None:
        save_quote_accounting(quote=quote, actor=creator, **accounting)
    if inquiry:
        inquiry.status = ShopInquiry.STATUS_QUOTED
        inquiry.quoted_amount = quote.total
        inquiry.assigned_coach = creator if creator.role in User.COACH_ROLE_VALUES else inquiry.assigned_coach
        inquiry.save(update_fields=["status", "quoted_amount", "assigned_coach", "updated_at"])
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
def update_quote(*, quote, customer, items, note="", accounting=None, actor=None, guest_name=""):
    quote = ShopQuote.objects.select_for_update().prefetch_related("items").get(pk=quote.pk)
    if quote.status in (ShopQuote.STATUS_PURCHASED, ShopQuote.STATUS_CANCELED) or quote.active_purchase:
        raise ValidationError("購入確定済みまたは取消済みの見積は編集できません。")
    rows = []
    for order, data in enumerate(items):
        item_data = {key: data.get(key) for key in (
            "description", "quantity", "list_price", "sale_price", "cost_price",
        )}
        if not can_manage_shop_accounting(actor):
            item_data["cost_price"] = data.get("_preserved_cost_price")
        item = ShopQuoteItem(quote=quote, sort_order=order, **item_data)
        item.full_clean()
        rows.append(item)
    if not rows:
        raise ValidationError("見積明細を1件以上入力してください。")
    quote.customer = customer
    quote.guest_name = guest_name
    if customer is None:
        quote.inquiry = None
    quote.note = (note or "").strip()
    if quote.status == ShopQuote.STATUS_PURCHASE_REQUESTED:
        quote.status = ShopQuote.STATUS_SENT
    quote.full_clean()
    quote.save(update_fields=["customer", "guest_name", "inquiry", "note", "status", "updated_at"])
    quote.items.all().delete()
    ShopQuoteItem.objects.bulk_create(rows)
    quote._prefetched_objects_cache.pop("items", None)
    if accounting is not None:
        save_quote_accounting(quote=quote, actor=actor, **accounting)
    if quote.inquiry_id:
        ShopInquiry.objects.filter(pk=quote.inquiry_id).update(
            status=ShopInquiry.STATUS_QUOTED, quoted_amount=quote.total,
        )
    return quote


@transaction.atomic
def confirm_quote_purchase(*, quote, actor):
    if not can_manage_shop_accounting(actor):
        raise PermissionError("adminのみ購入を確定できます。")
    quote = ShopQuote.objects.select_for_update().prefetch_related("items").get(pk=quote.pk)
    existing = ShopPurchase.objects.filter(
        quote=quote, status=ShopPurchase.STATUS_CONFIRMED,
    ).first()
    if existing:
        return existing, False
    if quote.status not in (ShopQuote.STATUS_SENT, ShopQuote.STATUS_PURCHASE_REQUESTED):
        raise ValidationError("見積済みまたは購入希望済みの見積のみ購入確定できます。")
    if not profit_summary(quote.items.all())["costs_complete"]:
        raise ValidationError("原価未入力の明細があるため購入を確定できません。")
    save_quote_accounting(
        quote=quote, actor=actor, procurement_coach=quote.procurement_coach_id,
        amounts={int(key): value for key, value in (quote.planned_profit_allocations or {}).items()},
    )
    quote.refresh_from_db()
    accounting = validate_quote_accounting_for_confirmation(quote)
    purchase = ShopPurchase(
        quote=quote, customer=quote.customer, guest_name=quote.guest_name,
        description="\n".join(i.description for i in quote.items.all()),
        quantity=sum(i.quantity for i in quote.items.all()), amount=accounting["sale_amount"],
        cost_total=accounting["purchase_cost"], procurement_coach=accounting["procurement_coach"],
        profit_amount_snapshot=accounting["profit"],
        profit_rate_snapshot=profit_rate(accounting["sale_amount"], accounting["profit"]),
        accounting_configured=True, note=quote.note, registered_by=actor,
    )
    purchase.full_clean()
    purchase.save()
    ShopRevenueAllocation.objects.bulk_create([
        ShopRevenueAllocation(purchase=purchase, coach=coach, amount=accounting["amounts"][coach.pk], created_by=actor)
        for coach in accounting["coaches"].values()
    ])
    ShopRevenueAllocationAudit.objects.create(
        purchase=purchase, allocation_snapshot=accounting_snapshot(
            sale_amount=accounting["sale_amount"], purchase_cost=accounting["purchase_cost"],
            procurement_coach=accounting["procurement_coach"], amounts=accounting["amounts"],
        ), changed_by=actor,
    )
    quote.status = ShopQuote.STATUS_PURCHASED
    quote.save(update_fields=["status", "updated_at"])
    if quote.inquiry_id:
        ShopInquiry.objects.filter(pk=quote.inquiry_id).update(
            status=ShopInquiry.STATUS_PURCHASED, purchased_at=purchase.purchased_at,
        )
    return purchase, True


@transaction.atomic
def create_direct_purchase(*, customer, actor, description, quantity, amount, note=""):
    purchase = ShopPurchase(customer=customer, registered_by=actor, description=description,
                            quantity=quantity, amount=amount, note=note)
    purchase.full_clean()
    purchase.save()
    return purchase


def profit_rate(amount, profit):
    return (Decimal(profit) * Decimal("100") / Decimal(amount)).quantize(
        Decimal("0.001"), rounding=ROUND_HALF_UP
    )


def accounting_snapshot(*, sale_amount, purchase_cost, procurement_coach, amounts):
    return {
        "sale_amount": sale_amount, "purchase_cost": purchase_cost,
        "procurement_coach_id": getattr(procurement_coach, "pk", procurement_coach),
        "profit_allocations": {str(key): int(value) for key, value in amounts.items()},
    }


def _main_coach_map():
    from .settlement_balance_policy import main_coaches
    return {coach.pk: coach for coach in main_coaches()}


def _normalize_main_coach_id(coach, main, *, required=False):
    coach_id = getattr(coach, "pk", coach)
    if coach_id in (None, ""):
        if required:
            raise ValidationError("仕入コーチはメインコーチから選択してください。")
        return None
    try:
        coach_id = int(coach_id)
    except (TypeError, ValueError):
        raise ValidationError("仕入コーチはメインコーチから選択してください。")
    if coach_id not in main:
        raise ValidationError("仕入コーチはメインコーチから選択してください。")
    return coach_id


@transaction.atomic
def save_quote_accounting(*, quote, actor, sale_amount=None, purchase_cost=None,
                          procurement_coach=None, amounts=None):
    if not can_manage_shop_accounting(actor):
        raise PermissionError("adminのみ内部精算情報を変更できます。")
    quote = ShopQuote.objects.select_for_update().get(pk=quote.pk)
    if quote.status in (ShopQuote.STATUS_PURCHASED, ShopQuote.STATUS_CANCELED) or quote.active_purchase:
        raise ValidationError("購入確定済みまたは取消済みの見積は編集できません。")
    main = _main_coach_map()
    coach_id = _normalize_main_coach_id(procurement_coach, main)
    canonical = profit_summary(quote.items.all())
    sale = canonical["revenue"]
    cost = canonical["cost"]
    if sale is not None and sale <= 0: raise ValidationError("売上額は1円以上にしてください。")
    if cost is not None and cost < 0: raise ValidationError("仕入額は0円以上にしてください。")
    if sale is not None and cost is not None and cost > sale:
        raise ValidationError("仕入額が売上額を超える販売は登録できません。")
    normalized = {coach_id: int((amounts or {}).get(coach_id, 0) or 0) for coach_id in main}
    if any(value < 0 for value in normalized.values()): raise ValidationError("按分額は0円以上にしてください。")
    before = accounting_snapshot(sale_amount=quote.accounting_sale_amount,
        purchase_cost=quote.accounting_purchase_cost, procurement_coach=quote.procurement_coach_id,
        amounts=quote.planned_profit_allocations or {})
    quote.accounting_sale_amount, quote.accounting_purchase_cost = sale, cost
    quote.procurement_coach = main.get(coach_id)
    quote.planned_profit_allocations = {str(key): value for key, value in normalized.items()}
    quote.save(update_fields=["accounting_sale_amount", "accounting_purchase_cost", "procurement_coach", "planned_profit_allocations", "updated_at"])
    after = accounting_snapshot(sale_amount=sale, purchase_cost=cost,
        procurement_coach=coach_id, amounts=normalized)
    if before != after:
        ShopRevenueAllocationAudit.objects.create(quote=quote, previous_snapshot=before,
            allocation_snapshot=after, changed_by=actor)
    return quote_accounting_summary(quote)


def quote_accounting_summary(quote):
    amounts = {int(key): int(value) for key, value in (quote.planned_profit_allocations or {}).items()}
    allocated = sum(amounts.values())
    canonical = profit_summary(quote.items.all())
    profit = quote.accounting_profit_amount if canonical["costs_complete"] else None
    rate = quote.accounting_profit_rate if canonical["costs_complete"] else None
    return {"profit": profit, "rate": rate, "allocated": allocated,
            "remaining": None if profit is None else profit - allocated,
            "complete": canonical["costs_complete"] and profit is not None
            and quote.procurement_coach_id is not None
            and allocated == profit and quote.accounting_sale_amount == quote.total}


def validate_quote_accounting_for_confirmation(quote):
    quote = ShopQuote.objects.select_related("procurement_coach").prefetch_related("items").get(pk=quote.pk)
    canonical = profit_summary(quote.items.all())
    if not canonical["costs_complete"]:
        raise ValidationError("原価未入力の明細があるため購入を確定できません。")
    if quote.accounting_sale_amount is None or quote.accounting_purchase_cost is None or not quote.procurement_coach_id:
        raise ValidationError("購入確定前に内部精算情報の売上額・仕入額・仕入コーチを入力してください。")
    if quote.accounting_sale_amount != canonical["revenue"]:
        raise ValidationError(
            f"内部精算情報の売上額を見積合計と一致させてください。見積合計: {canonical['revenue']:,}円"
        )
    if quote.accounting_purchase_cost != canonical["cost"]:
        raise ValidationError(
            f"内部精算情報の仕入額を原価合計と一致させてください。原価合計: {canonical['cost']:,}円"
        )
    main = _main_coach_map()
    amounts = {int(key): int(value) for key, value in (quote.planned_profit_allocations or {}).items()}
    if set(amounts) != set(main): raise ValidationError("利益分配はメインコーチ全員分を指定してください。")
    profit = quote.accounting_profit_amount
    allocated = sum(amounts.values())
    if allocated != profit:
        raise ValidationError(f"利益分配額の合計が利益額と一致していません。利益額: {profit:,}円 分配合計: {allocated:,}円 残額: {profit - allocated:,}円")
    return {"sale_amount": quote.accounting_sale_amount, "purchase_cost": quote.accounting_purchase_cost,
            "procurement_coach": main[quote.procurement_coach_id], "profit": profit,
            "amounts": amounts, "coaches": main}


def _ensure_purchase_month_open(purchase):
    from .models import ensure_accounting_month_is_open
    ensure_accounting_month_is_open(purchase.purchased_at)


@transaction.atomic
def save_allocations(
    *, purchase, actor, amounts, sale_amount=None, purchase_cost=None,
    procurement_coach=None
):
    if not can_manage_shop_accounting(actor):
        raise PermissionError("adminのみ売上按分を変更できます。")
    purchase = ShopPurchase.objects.select_for_update().get(pk=purchase.pk)
    _ensure_purchase_month_open(purchase)
    if purchase.status != ShopPurchase.STATUS_CONFIRMED:
        raise ValidationError("購入確定済みのShop売上だけ按分できます。")
    sale_amount = int(purchase.amount if sale_amount is None else sale_amount)
    purchase_cost = purchase.cost_total if purchase_cost is None else purchase_cost
    if purchase_cost is None:
        raise ValidationError("仕入額を入力してください。")
    purchase_cost = int(purchase_cost)
    if sale_amount <= 0:
        raise ValidationError("売上額は1円以上にしてください。")
    if purchase_cost < 0:
        raise ValidationError("仕入額は0円以上にしてください。")
    if purchase_cost > sale_amount:
        raise ValidationError("仕入額が売上額を超える販売は登録できません。")
    if procurement_coach is None:
        procurement_coach = purchase.procurement_coach
    main = _main_coach_map()
    procurement_id = _normalize_main_coach_id(procurement_coach, main, required=True)
    normalized = {int(coach_id): int(amount or 0) for coach_id, amount in amounts.items()}
    if any(amount < 0 for amount in normalized.values()):
        raise ValidationError("按分額は0円以上にしてください。")
    profit = sale_amount - purchase_cost
    if sum(normalized.values()) != profit:
        raise ValidationError("利益分配額の合計を利益額と一致させてください。")
    if set(normalized) != set(main):
        raise ValidationError("利益分配はメインコーチ全員分を指定してください。")
    coaches = main
    before = accounting_snapshot(sale_amount=purchase.amount, purchase_cost=purchase.cost_total,
        procurement_coach=purchase.procurement_coach_id,
        amounts={a.coach_id: a.amount for a in purchase.allocations.all()})
    purchase.amount = sale_amount
    purchase.cost_total = purchase_cost
    purchase.procurement_coach = main[procurement_id]
    purchase.profit_amount_snapshot = profit
    purchase.profit_rate_snapshot = profit_rate(sale_amount, profit)
    purchase.accounting_configured = True
    purchase.full_clean()
    purchase.save(update_fields=["amount", "cost_total", "procurement_coach",
        "profit_amount_snapshot", "profit_rate_snapshot", "accounting_configured", "updated_at"])
    purchase.allocations.exclude(coach_id__in=normalized).delete()
    for coach_id, amount in normalized.items():
        ShopRevenueAllocation.objects.update_or_create(
            purchase=purchase, coach=coaches[coach_id], defaults={"amount": amount, "created_by": actor})
    after = accounting_snapshot(sale_amount=sale_amount, purchase_cost=purchase_cost,
        procurement_coach=procurement_id, amounts=normalized)
    ShopRevenueAllocationAudit.objects.create(purchase=purchase, previous_snapshot=before,
        allocation_snapshot=after, changed_by=actor)
    return allocation_summary(purchase)


def allocation_summary(purchase):
    allocated = purchase.allocations.aggregate(total=Sum("amount"))["total"] or 0
    profit = purchase.profit_amount_snapshot
    if profit is None and purchase.cost_total is not None:
        profit = int(purchase.amount) - int(purchase.cost_total)
    remaining = int(profit or 0) - int(allocated)
    return {"allocated": allocated, "remaining": remaining, "profit": profit,
            "complete": purchase.accounting_configured and purchase.status == ShopPurchase.STATUS_CONFIRMED and remaining == 0}


def monthly_shop_allocations(year, month):
    rows = (ShopRevenueAllocation.objects.filter(
        purchase__status=ShopPurchase.STATUS_CONFIRMED,
        purchase__accounting_configured=True,
        purchase__purchased_at__year=year, purchase__purchased_at__month=month,
    ).values("coach_id").annotate(total=Sum("amount")))
    return {row["coach_id"]: int(row["total"] or 0) for row in rows}


def monthly_shop_procurement_reimbursements(year, month):
    rows = (ShopPurchase.objects.filter(
        status=ShopPurchase.STATUS_CONFIRMED, accounting_configured=True,
        purchased_at__year=year, purchased_at__month=month,
    ).values("procurement_coach_id").annotate(total=Sum("cost_total")))
    return {row["procurement_coach_id"]: int(row["total"] or 0) for row in rows}


def monthly_shop_cash_total(year, month):
    return int(ShopPurchase.objects.filter(
        status=ShopPurchase.STATUS_CONFIRMED, accounting_configured=True,
        purchased_at__year=year, purchased_at__month=month,
    ).aggregate(total=Sum("amount"))["total"] or 0)


@transaction.atomic
def cancel_purchase(*, purchase, actor):
    purchase = ShopPurchase.objects.select_for_update().get(pk=purchase.pk)
    _ensure_purchase_month_open(purchase)
    if purchase.status == ShopPurchase.STATUS_CANCELED:
        raise ValidationError("このShop販売は既に取り消されています。")
    purchase.status = ShopPurchase.STATUS_CANCELED
    purchase.save(update_fields=["status", "updated_at"])
    return purchase


@transaction.atomic
def rollback_purchase_to_quote(*, purchase, actor, reason):
    if not can_manage_shop_accounting(actor):
        raise PermissionError("adminのみ見積へ差し戻せます。")
    reason = (reason or "").strip()
    if not reason:
        raise ValidationError("差し戻し理由を入力してください。")
    purchase = ShopPurchase.objects.select_for_update().get(pk=purchase.pk)
    if not purchase.quote_id:
        raise ValidationError("見積に紐づかない購入は差し戻せません。")
    quote = ShopQuote.objects.select_for_update().get(pk=purchase.quote_id)
    _ensure_purchase_month_open(purchase)
    if purchase.status != ShopPurchase.STATUS_CONFIRMED:
        raise ValidationError("購入確定中のShop購入だけを差し戻せます。")
    active_ids = list(ShopPurchase.objects.filter(
        quote=quote, status=ShopPurchase.STATUS_CONFIRMED,
    ).values_list("pk", flat=True))
    if active_ids != [purchase.pk]:
        raise ValidationError("同一見積の購入確定状態が不整合です。")
    snapshot = accounting_snapshot(
        sale_amount=purchase.amount, purchase_cost=purchase.cost_total,
        procurement_coach=purchase.procurement_coach_id,
        amounts={row.coach_id: row.amount for row in purchase.allocations.all()},
    )
    snapshot.update({
        "purchase_id": purchase.pk, "quote_id": quote.pk,
        "profit_amount": purchase.profit_amount_snapshot,
        "profit_rate": str(purchase.profit_rate_snapshot) if purchase.profit_rate_snapshot is not None else None,
        "status": purchase.status,
    })
    ShopRevenueAllocationAudit.objects.create(
        purchase=purchase, quote=quote, previous_snapshot=snapshot,
        allocation_snapshot=snapshot, event_type=ShopRevenueAllocationAudit.EVENT_ROLLBACK,
        reason=reason, changed_by=actor,
    )
    purchase.status = ShopPurchase.STATUS_REVERTED
    purchase.save(update_fields=["status", "updated_at"])
    quote.status = ShopQuote.STATUS_SENT
    quote.save(update_fields=["status", "updated_at"])
    if quote.inquiry_id:
        ShopInquiry.objects.filter(pk=quote.inquiry_id).update(
            status=ShopInquiry.STATUS_QUOTED, purchased_at=None,
            quoted_amount=quote.total,
        )
    return purchase
