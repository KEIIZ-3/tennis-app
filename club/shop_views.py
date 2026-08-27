from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.http import HttpResponse, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render

from .models import ShopEstimateRequest, ShopInquiry, ShopPurchase, ShopQuote, User
from .shop_forms import DirectPurchaseForm, ShopInquiryForm, ShopQuoteForm, ShopQuoteItemFormSet
from .shop_pdf import build_quote_pdf
from .shop_service import (allocation_summary, confirm_quote_purchase, create_direct_purchase,
                           create_inquiry, create_quote, request_purchase, save_allocations)


def _staff(user): return bool(user.is_staff or user.is_superuser)
def _coach(user): return _staff(user) or user.role in User.COACH_ROLE_VALUES


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
    return render(request, "shop/quote_detail.html", {"quote": quote, "can_manage": _coach(request.user)})


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
    form = ShopQuoteForm(request.POST or None, initial=initial)
    formset = ShopQuoteItemFormSet(request.POST or None, prefix="items")
    if request.method == "POST" and form.is_valid() and formset.is_valid():
        items = [row for row in formset.cleaned_data if row]
        try:
            quote = create_quote(customer=form.cleaned_data["customer"], creator=request.user,
                inquiry=form.cleaned_data.get("inquiry"), note=form.cleaned_data["note"], items=items)
        except ValidationError as exc:
            form.add_error(None, "; ".join(exc.messages))
        else:
            return redirect("club:shop_quote_detail", pk=quote.pk)
    return render(request, "shop/quote_form.html", {"form": form, "formset": formset})


@login_required
def quote_confirm(request, pk):
    if not _coach(request.user): return HttpResponseForbidden()
    if request.method != "POST": return HttpResponse(status=405)
    quote = get_object_or_404(ShopQuote, pk=pk)
    confirm_quote_purchase(quote=quote, actor=request.user)
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
    coaches = User.objects.filter(is_active=True, role__in=User.COACH_ROLE_VALUES).order_by("full_name", "username")
    current = {a.coach_id: a for a in purchase.allocations.all()}
    if request.method == "POST":
        amounts = {coach.pk: request.POST.get(f"coach_{coach.pk}", 0) for coach in coaches}
        try: save_allocations(purchase=purchase, actor=request.user, amounts=amounts)
        except (ValidationError, ValueError) as exc: messages.error(request, str(exc))
        else: messages.success(request, "売上按分を保存しました。")
        return redirect("club:shop_allocation", pk=pk)
    rows = [{"coach": c, "amount": current[c.pk].amount if c.pk in current else 0,
             "percentage": current[c.pk].percentage if c.pk in current else 0} for c in coaches]
    return render(request, "shop/allocation.html", {"purchase": purchase, "rows": rows, "summary": allocation_summary(purchase)})
