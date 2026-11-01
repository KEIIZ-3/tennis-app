"""Read-only Nishina Park court payment submission list."""

from calendar import monthrange
from datetime import datetime
from io import BytesIO
from urllib.parse import urlencode

from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, HttpResponseForbidden
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import KeepTogether, Paragraph, SimpleDocTemplate, Spacer

from . import lesson_execution
from .models import CoachAvailability, Court
from .shop_pdf import FONT_NAME, _paragraph, _register_font


TITLE = "西猪名公園 コート支払一覧"
WEEKDAYS = "月火水木金土日"


def _is_allowed(user):
    return bool(
        user.is_authenticated
        and (
            getattr(user, "role", "") in ("coach", "contractor_coach")
            or getattr(user, "is_staff", False)
            or getattr(user, "is_superuser", False)
        )
    )


def _selected_month(request):
    today = timezone.localdate()
    try:
        year = int(request.GET.get("year", today.year))
        month = int(request.GET.get("month", today.month))
        monthrange(year, month)
    except (TypeError, ValueError):
        return today.year, today.month
    return year, month


def _month_bounds(year, month):
    start = timezone.make_aware(datetime(year, month, 1))
    if month == 12:
        end = timezone.make_aware(datetime(year + 1, 1, 1))
    else:
        end = timezone.make_aware(datetime(year, month + 1, 1))
    return start, end


def _month_pair(year, month, offset):
    value = year * 12 + month - 1 + offset
    return value // 12, value % 12 + 1


def _month_url(name, year, month):
    return f"{reverse(name)}?{urlencode({'year': year, 'month': month})}"


def payment_rows(year, month):
    """Return the canonical, stable list without modifying application data."""
    start, end = _month_bounds(year, month)
    availabilities = list(
        CoachAvailability.objects.filter(
            court__court_type=Court.COURT_SONO,
            start_at__gte=start,
            start_at__lt=end,
        )
        .select_related("court_booking_account_coach")
        .order_by("start_at", "id")
    )
    excluded_ids = set(lesson_execution.non_occupying_availability_ids(availabilities))
    rows = []
    for availability in availabilities:
        if availability.pk in excluded_ids:
            continue
        local_start = timezone.localtime(availability.start_at)
        local_end = timezone.localtime(availability.end_at)
        rows.append(
            {
                "availability_id": availability.pk,
                "date_time": (
                    f"{local_start.month}/{local_start.day}"
                    f"（{WEEKDAYS[local_start.weekday()]}） "
                    f"{local_start:%H:%M}〜{local_end:%H:%M}"
                ),
                "reservation_number": availability.court_reservation_number or "未設定",
                "reservation_number_missing": not bool(availability.court_reservation_number),
                "booker": availability.court_booking_account_display(),
            }
        )
    return rows


def _pdf_url(request, year, month):
    relative = _month_url("club:court_payment_list_pdf", year, month)
    absolute = request.build_absolute_uri(relative)
    if "line/" in request.headers.get("User-Agent", "").lower():
        absolute += "&openExternalBrowser=1"
    return absolute


def _context(request, year, month, rows):
    previous_year, previous_month = _month_pair(year, month, -1)
    next_year, next_month = _month_pair(year, month, 1)
    return {
        "title": TITLE,
        "selected_year": year,
        "selected_month": month,
        "rows": rows,
        "previous_url": _month_url("club:court_payment_list", previous_year, previous_month),
        "next_url": _month_url("club:court_payment_list", next_year, next_month),
        "pdf_url": _pdf_url(request, year, month),
    }


@login_required
def court_payment_list(request):
    if not _is_allowed(request.user):
        return HttpResponseForbidden()
    year, month = _selected_month(request)
    rows = payment_rows(year, month)
    return render(request, "coach/court_payment_list.html", _context(request, year, month, rows))


def build_payment_list_pdf(year, month, rows):
    _register_font()
    buffer = BytesIO()
    document = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=15 * mm,
        bottomMargin=15 * mm,
        title=f"{TITLE} {year}年{month}月",
        author="Play Design Tennis",
    )
    styles = getSampleStyleSheet()
    base = ParagraphStyle(
        "CourtPayment",
        parent=styles["Normal"],
        fontName=FONT_NAME,
        fontSize=10.5,
        leading=15,
        textColor=colors.HexColor("#1f2937"),
    )
    heading = ParagraphStyle("CourtPaymentHeading", parent=base, fontSize=17, leading=22)
    month_style = ParagraphStyle("CourtPaymentMonth", parent=base, fontSize=12, leading=18)
    number_style = ParagraphStyle(
        "CourtPaymentNumber", parent=base, fontSize=13, leading=18, spaceBefore=1.5 * mm
    )
    missing_style = ParagraphStyle(
        "CourtPaymentMissing", parent=number_style, textColor=colors.HexColor("#9a6700")
    )
    story = [
        _paragraph(TITLE, heading),
        _paragraph(f"{year}年{month}月", month_style),
        Spacer(1, 5 * mm),
    ]
    for row in rows:
        story.append(
            KeepTogether(
                [
                    _paragraph(row["date_time"], base),
                    _paragraph(
                        f"予約番号：{row['reservation_number']}",
                        missing_style if row["reservation_number_missing"] else number_style,
                    ),
                    _paragraph(f"予約者：{row['booker']}", base),
                    Spacer(1, 5 * mm),
                ]
            )
        )
    document.build(story)
    return buffer.getvalue()


@login_required
def court_payment_list_pdf(request):
    if not _is_allowed(request.user):
        return HttpResponseForbidden()
    year, month = _selected_month(request)
    response = HttpResponse(
        build_payment_list_pdf(year, month, payment_rows(year, month)),
        content_type="application/pdf",
    )
    response["Content-Disposition"] = (
        f'inline; filename="court-payment-list-{year:04d}-{month:02d}.pdf"'
    )
    return response
