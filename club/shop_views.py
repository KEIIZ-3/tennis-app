from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.http import HttpResponse, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render

from .models import ShopEstimateRequest, ShopInquiry, ShopPurchase, ShopQuote, User
from .shop_forms import DirectPurchaseForm, ShopInquiryForm, ShopQuoteForm, ShopQuoteItemFormSet
from .shop_pdf import build_quote_pdf
from .shop_service import (allocation_summary, confirm_quote_purchase, create_direct_purchase,
                           cancel_purchase, create_inquiry, create_quote, request_purchase, save_allocations,
                           quote_accounting_summary, update_quote)
from .settlement_balance_policy import main_coaches


def _staff(user): return bool(user.is_staff or user.is_superuser)
def _coach(user): return _staff(user) or user.role in User.COACH_ROLE_VALUES


def _accounting_data(request, coaches):
    return {
        "sale_amount": request.POST.get("accounting_sale_amount"),
        "purchase_cost": request.POST.get("accounting_purchase_cost"),
        "procurement_coach": request.POST.get("procurement_coach"),
        "amounts": {coach.pk: request.POST.get(f"accounting_coach_{coach.pk}", 0) for coach in coaches},
    }


@login_required
def shop_top(request):
    form = ShopInquiryForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        create_inquiry(customer=request.user, wanted_item=form.cleaned_data["wanted_item"])
        messages.success(request, "価格問い合わせを受け付けました。")
        return redirect("club:shop_estimate_history")
    return render(request, "shop/estimate.html", {"form": form, "is_shop_staff": _coach(request.user)})


@login_required
def shop_history(request):
    return render(request, "shop/history.html", {
        "inquiries": ShopInquiry.objects.filter(customer=request.user).prefetch_related("quotes"),
        "quotes": ShopQuote.objects.filter(customer=request.user).prefetch_related("items"),
        "purchases": ShopPurchase.objects.filter(customer=request.user),
        "legacy_requests": ShopEstimateRequest.objects.filter(user=request.user),
    })


@login_required
def quote_detail(request, pk):
    query = ShopQuote.objects.prefetch_related("items")
    quote = get_object_or_404(query if _coach(request.user) else query.filter(customer=request.user), pk=pk)
    coaches = list(main_coaches()) if _coach(request.user) else []
    amounts = {int(key): int(value) for key, value in (quote.planned_profit_allocations or {}).items()}
    return render(request, "shop/quote_detail.html", {"quote": quote, "can_manage": _coach(request.user),
        "can_edit_accounting": _staff(request.user), "accounting": quote_accounting_summary(quote),
        "accounting_rows": [{"coach": coach, "amount": amounts.get(coach.pk, 0)} for coach in coaches]})


@login_required
def quote_purchase_request(request, pk):
    if request.method != "POST": return HttpResponse(status=405)
    quote = get_object_or_404(ShopQuote, pk=pk, customer=request.user)
    try: request_purchase(quote=quote, customer=request.user)
    except ValidationError as exc: messages.error(request, "; ".join(exc.messages))
    return redirect("club:shop_quote_detail", pk=pk)


@login_required
def coach_shop(request):
    if not _coach(request.user): return HttpResponseForbidden()
    return render(request, "shop/coach_dashboard.html", {
        "inquiries": ShopInquiry.objects.select_related("customer", "assigned_coach")[:100],
        "quotes": ShopQuote.objects.select_related("customer").prefetch_related("items")[:100],
        "purchases": ShopPurchase.objects.select_related("customer")[:100],
    })


@login_required
def quote_create(request):
    if not _coach(request.user): return HttpResponseForbidden()
    initial = {}
    inquiry = None
    if request.GET.get("inquiry"):
        inquiry = get_object_or_404(ShopInquiry, pk=request.GET["inquiry"])
        initial = {"customer": inquiry.customer, "inquiry": inquiry}
    coaches = list(main_coaches()) if _staff(request.user) else []
    form = ShopQuoteForm(request.POST or None, initial=initial, can_edit_accounting=_staff(request.user))
    formset = ShopQuoteItemFormSet(request.POST or None, prefix="items")
    if request.method == "POST" and form.is_valid() and formset.is_valid():
        items = [row for row in formset.cleaned_data if row]
        try:
            quote = create_quote(customer=form.cleaned_data["customer"], creator=request.user,
                inquiry=form.cleaned_data.get("inquiry"), note=form.cleaned_data["note"], items=items,
                accounting=_accounting_data(request, coaches) if _staff(request.user) else None)
        except (ValidationError, ValueError) as exc:
            form.add_error(None, "; ".join(exc.messages))
        else:
            return redirect("club:shop_quote_detail", pk=quote.pk)
    return render(request, "shop/quote_form.html", {"form": form, "formset": formset, "is_edit": False,
        "can_edit_accounting": _staff(request.user), "accounting_rows": [{"coach": c, "amount": 0} for c in coaches]})


@login_required
def quote_edit(request, pk):
    if not _coach(request.user): return HttpResponseForbidden()
    quote = get_object_or_404(ShopQuote.objects.prefetch_related("items"), pk=pk)
    if quote.status in (ShopQuote.STATUS_PURCHASED, ShopQuote.STATUS_CANCELED) or hasattr(quote, "purchase"):
        messages.error(request, "購入確定済みまたは取消済みの見積は編集できません。")
        return redirect("club:shop_quote_detail", pk=pk)
    initial = {"customer": quote.customer, "inquiry": quote.inquiry, "note": quote.note,
        "accounting_sale_amount": quote.accounting_sale_amount,
        "accounting_purchase_cost": quote.accounting_purchase_cost,
        "procurement_coach": quote.procurement_coach_id}
    item_initial = [{
        "description": item.description, "quantity": item.quantity,
        "list_price": item.list_price, "sale_price": item.sale_price,
        "discount_rate": item.discount_rate, "cost_price": item.cost_price,
        "pricing_source": "sale",
    } for item in quote.items.all()]
    coaches = list(main_coaches()) if _staff(request.user) else []
    form = ShopQuoteForm(request.POST or None, initial=initial, can_edit_accounting=_staff(request.user))
    formset = ShopQuoteItemFormSet(request.POST or None, prefix="items", initial=item_initial)
    if request.method == "POST" and form.is_valid() and formset.is_valid():
        items = [row for row in formset.cleaned_data if row and not row.get("DELETE")]
        try:
            update_quote(quote=quote, customer=form.cleaned_data["customer"],
                         note=form.cleaned_data["note"], items=items, actor=request.user,
                         accounting=_accounting_data(request, coaches) if _staff(request.user) else None)
        except (ValidationError, ValueError) as exc:
            form.add_error(None, "; ".join(exc.messages))
        else:
            messages.success(request, "見積を更新しました。購入希望済みの場合は、お客様の再確認が必要です。")
            return redirect("club:shop_quote_detail", pk=quote.pk)
    amounts = {int(key): int(value) for key, value in (quote.planned_profit_allocations or {}).items()}
    return render(request, "shop/quote_form.html", {
        "form": form, "formset": formset, "is_edit": True, "quote": quote,
        "can_edit_accounting": _staff(request.user),
        "accounting_rows": [{"coach": c, "amount": amounts.get(c.pk, 0)} for c in coaches],
    })


@login_required
def quote_confirm(request, pk):
    if not _coach(request.user): return HttpResponseForbidden()
    if request.method != "POST": return HttpResponse(status=405)
    quote = get_object_or_404(ShopQuote, pk=pk)
    try:
        confirm_quote_purchase(quote=quote, actor=request.user)
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
        return redirect("club:shop_quote_detail", pk=pk)
    return redirect("club:shop_coach")


@login_required
def direct_purchase(request):
    if not _coach(request.user): return HttpResponseForbidden()
    form = DirectPurchaseForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        create_direct_purchase(customer=form.cleaned_data["customer"], actor=request.user,
            description=form.cleaned_data["description"], quantity=form.cleaned_data["quantity"],
            amount=form.cleaned_data["amount"], note=form.cleaned_data["note"])
        return redirect("club:shop_coach")
    return render(request, "shop/direct_purchase.html", {"form": form})


@login_required
def quote_pdf(request, pk):
    quote = get_object_or_404(ShopQuote.objects.prefetch_related("items"), pk=pk)
    if not _coach(request.user) and quote.customer_id != request.user.pk: return HttpResponseForbidden()
    response = HttpResponse(build_quote_pdf(quote), content_type="application/pdf")
    response["Content-Disposition"] = f'inline; filename="{quote.quote_number}.pdf"'
    return response


@login_required
def allocation_edit(request, pk):
    if not _staff(request.user): return HttpResponseForbidden()
    purchase = get_object_or_404(ShopPurchase.objects.prefetch_related("allocations"), pk=pk)
    coaches = main_coaches()
    current = {a.coach_id: a for a in purchase.allocations.all()}
    if request.method == "POST":
        amounts = {coach.pk: request.POST.get(f"coach_{coach.pk}", 0) for coach in coaches}
        try: save_allocations(
            purchase=purchase, actor=request.user, amounts=amounts,
            sale_amount=request.POST.get("sale_amount"),
            purchase_cost=request.POST.get("purchase_cost"),
            procurement_coach=request.POST.get("procurement_coach"),
        )
        except (ValidationError, ValueError) as exc: messages.error(request, str(exc))
        else: messages.success(request, "売上按分を保存しました。")
        return redirect("club:shop_allocation", pk=pk)
    rows = [{"coach": c, "amount": current[c.pk].amount if c.pk in current else 0,
             "percentage": current[c.pk].percentage if c.pk in current else 0} for c in coaches]
    return render(request, "shop/allocation.html", {"purchase": purchase, "rows": rows, "summary": allocation_summary(purchase), "coaches": coaches})


@login_required
def purchase_cancel(request, pk):
    if not _staff(request.user): return HttpResponseForbidden()
    if request.method != "POST": return HttpResponse(status=405)
    purchase = get_object_or_404(ShopPurchase, pk=pk)
    try: cancel_purchase(purchase=purchase, actor=request.user)
    except ValidationError as exc: messages.error(request, "; ".join(exc.messages))
    else: messages.success(request, "Shop販売を取り消しました。")
    return redirect("club:shop_coach")
