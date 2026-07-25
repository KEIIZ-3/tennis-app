import re

from django.contrib.auth.decorators import login_required
from django.db import models
from django.http import HttpResponseForbidden
from django.utils import timezone

from . import reservation_cancel_override, views
from .models import FixedLesson, Reservation


def _replace_html(response, transform):
    content_type = response.get("Content-Type", "")
    if response.status_code != 200 or "text/html" not in content_type:
        return response

    try:
        charset = response.charset or "utf-8"
        html = response.content.decode(charset)
    except Exception:
        return response

    updated_html = transform(html)
    if updated_html == html:
        return response

    response.content = updated_html.encode(charset)
    if response.has_header("Content-Length"):
        response["Content-Length"] = str(len(response.content))
    return response


def _improve_ticket_page(html):
    html = html.replace(
        "{{ user.display_name }} さんのチケット残数、保有内訳、消費履歴を確認できます。",
        "{{ user.display_name }} さんの現在のチケット残数、予約時に差し引かれた内訳、返却履歴を確認できます。",
    )

    html = html.replace(
        '<a href="#ticket-consumptions" class="ticket-jump-link">消費内訳</a>',
        '<a href="#ticket-consumptions" class="ticket-jump-link">予約分の差し引き</a>',
    )

    html = html.replace(
        "残数、保有内訳、消費履歴を確認できます。残数が少ない場合は追加購入をご相談ください。",
        "現在の残数には、予約済みレッスンで使用するチケットの差し引きがすでに反映されています。",
    )

    notice = """
<div class="card">
  <div style="padding:16px; border:1px solid #bfdbfe; background:#eff6ff; border-radius:16px; color:#1e3a8a;">
    <div style="font-weight:900; font-size:16px; margin-bottom:7px;">予約済みレッスンのチケットについて</div>
    <div style="font-size:13px; line-height:1.75; font-weight:700;">
      チケットはレッスン当日ではなく、予約が成立した時点で残数から差し引かれます。<br>
      そのため、画面上の「現在の残数」は、今後の予約分を差し引いた後の枚数です。<br>
      キャンセルまたは雨天中止になった場合は、使用したチケットへ自動で返却されます。
    </div>
  </div>
</div>
""".strip()

    marker = '<div class="ticket-stat-grid">'
    if notice not in html and marker in html:
        html = html.replace(marker, notice + "\n\n" + marker, 1)

    html = html.replace(
        '<h2 style="margin-top:0;">最近のチケット消費内訳</h2>',
        '<h2 style="margin-top:0;">予約時に差し引かれたチケット</h2>\n  <p class="muted" style="margin:-4px 0 14px; font-size:13px; line-height:1.7;">今後の予約を含め、予約成立時に差し引かれたチケットを表示しています。</p>',
    )
    html = html.replace(">使用中<", ">差し引き済み<")
    return html


def _simplify_reservation_page(html):
    html = html.replace(
        "今後の予約、キャンセル待ち、過去の履歴をまとめて確認できます。家族で予約した場合も、実際に参加する受講者名を確認できます。",
        "今後の予約、キャンセル待ち、参加済みの履歴を確認できます。キャンセル済みの予約は一覧に表示しません。",
    )
    html = html.replace(
        "<div>消費チケット：{{ reservation.tickets_used }}枚</div>",
        "<div>予約時に差し引き済み：{{ reservation.tickets_used }}枚</div>",
    )

    canceled_section_pattern = re.compile(
        r'<section class="section-card">\s*'
        r'<h2 class="section-title"><span>キャンセル済み・処理済み</span>.*?'
        r'</section>',
        re.DOTALL,
    )
    html = canceled_section_pattern.sub("", html)
    return html


def _sync_missing_fixed_reservations_for_member(user):
    """
    固定参加登録はあるものの、過去の定員判定不整合などで開催日別の
    Reservation が欠けた会員だけを予約確認表示前に補完する。
    """
    fixed_lessons = (
        FixedLesson.objects.filter(
            is_active=True,
            members=user,
            court__isnull=False,
        )
        .select_related("coach", "coach_2", "coach_3", "court")
        .distinct()
    )

    for fixed_lesson in fixed_lessons:
        occurrence_datetimes = [
            fixed_lesson._build_datetimes_for_date(target_date)
            for target_date in fixed_lesson.scheduled_occurrence_dates()
            if target_date >= timezone.localdate()
        ]
        if not occurrence_datetimes:
            continue

        active_or_member_canceled_starts = set(
            Reservation.objects.filter(
                user=user,
                fixed_lesson=fixed_lesson,
                start_at__in=[start_at for start_at, _end_at in occurrence_datetimes],
            )
            .filter(
                models.Q(status__in=[
                    Reservation.STATUS_ACTIVE,
                    Reservation.STATUS_PENDING,
                    Reservation.STATUS_RAIN_CANCELED,
                ])
                | models.Q(
                    status=Reservation.STATUS_CANCELED,
                    cancellation_reason="会員が予約確認画面からキャンセル",
                )
            )
            .values_list("start_at", flat=True)
        )
        if all(
            start_at in active_or_member_canceled_starts
            for start_at, _end_at in occurrence_datetimes
        ):
            continue

        fixed_lesson.sync_future_reservations(created_by=user)


def _improve_lesson_calendar(html):
    replacement_notice = """
<div class="ticket-notice" style="border-color:#60a5fa; background:#eff6ff; color:#1e3a8a;">
  <span class="ticket-notice-icon" style="background:#2563eb;">i</span>
  <div>
    <p class="ticket-notice-title" style="color:#1e3a8a;">🎫 チケットについて</p>
    <p class="ticket-notice-text">
      <strong>チケットが0枚でもレッスンをご予約いただけます。</strong><br>
      ご予約時にチケットをお持ちでなくても問題ありません。<br>
      レッスン当日に会場で現金にてチケットをご購入いただけます。<br>
      ご購入後にスタッフがチケットを反映いたします。
    </p>
    <p class="ticket-notice-title" style="color:#1e3a8a; margin-top:12px;">📅 ご予約について</p>
    <p class="ticket-notice-text">
      コート手配の都合上、レッスンのご予約は開催日の1週間前までにお願いいたします。
    </p>
  </div>
</div>
""".strip()

    notice_pattern = re.compile(
        r'<div class="ticket-notice">\s*'
        r'<span class="ticket-notice-icon">✓</span>\s*'
        r'<div>\s*'
        r'<p class="ticket-notice-title">チケットが足りない場合もご予約いただけます。</p>\s*'
        r'<p class="ticket-notice-text">.*?</p>\s*'
        r'</div>\s*'
        r'</div>',
        re.DOTALL,
    )
    html = notice_pattern.sub(replacement_notice, html, count=1)

    return html


def lesson_calendar_view(request):
    response = views.lesson_calendar_view(request)
    return _replace_html(response, _improve_lesson_calendar)


@login_required
def tickets_view(request):
    response = views.tickets_view(request)
    return _replace_html(response, _improve_ticket_page)


@login_required
def reservation_list(request):
    if getattr(request.user, "role", "") != "member":
        return HttpResponseForbidden("予約確認は会員専用です。")

    _sync_missing_fixed_reservations_for_member(request.user)
    response = reservation_cancel_override.reservation_list(request)
    return _replace_html(response, _simplify_reservation_page)
