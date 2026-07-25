import re

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpResponseForbidden
from django.shortcuts import redirect

from . import reservation_cancel_override, views
from .fixed_lesson_membership_service import synchronize_fixed_lesson_membership
from .models import FixedLesson


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
    return canceled_section_pattern.sub("", html)


def _member_fixed_lessons(user):
    return (
        FixedLesson.objects.filter(
            is_active=True,
            members=user,
            court__isnull=False,
        )
        .select_related("coach", "coach_2", "coach_3", "court")
        .distinct()
    )


def _synchronize_member_fixed_lessons(user):
    """固定メンバー設定と開催日別予約を同じ同期サービスで検証する。"""
    for fixed_lesson in _member_fixed_lessons(user):
        synchronize_fixed_lesson_membership(
            fixed_lesson.pk,
            created_by=user,
        )


def _synchronize_posted_fixed_lesson(request):
    fixed_lesson_id = (request.POST.get("fixed_lesson_id") or "").strip()
    if not fixed_lesson_id:
        return
    fixed_lesson = FixedLesson.objects.filter(
        pk=fixed_lesson_id,
        is_active=True,
    ).first()
    if fixed_lesson is None:
        return
    synchronize_fixed_lesson_membership(
        fixed_lesson.pk,
        created_by=request.user if request.user.is_authenticated else None,
    )


def _synchronize_requested_fixed_lesson(request):
    fixed_lesson_id = (request.GET.get("fixed_lesson_id") or "").strip()
    if not fixed_lesson_id:
        return
    fixed_lesson = FixedLesson.objects.filter(
        pk=fixed_lesson_id,
        is_active=True,
    ).first()
    if fixed_lesson is None:
        return
    synchronize_fixed_lesson_membership(
        fixed_lesson.pk,
        created_by=request.user if request.user.is_authenticated else None,
    )


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
    return notice_pattern.sub(replacement_notice, html, count=1)


def lesson_calendar_view(request):
    try:
        if request.method == "POST" and request.user.is_authenticated:
            _synchronize_posted_fixed_lesson(request)
    except Exception as exc:
        messages.error(request, f"固定レッスンの予約整合性を確認できませんでした: {exc}")
        return redirect("club:reservation_list")

    response = views.lesson_calendar_view(request)
    return _replace_html(response, _improve_lesson_calendar)


@login_required
def lesson_reservation_confirm(request):
    try:
        _synchronize_requested_fixed_lesson(request)
    except Exception as exc:
        messages.error(request, f"固定レッスンの予約整合性を確認できませんでした: {exc}")
        return redirect("club:reservation_list")
    return views.lesson_reservation_confirm(request)


@login_required
def tickets_view(request):
    response = views.tickets_view(request)
    return _replace_html(response, _improve_ticket_page)


@login_required
def reservation_list(request):
    if getattr(request.user, "role", "") != "member":
        return HttpResponseForbidden("予約確認は会員専用です。")

    try:
        _synchronize_member_fixed_lessons(request.user)
    except Exception as exc:
        messages.error(request, f"固定レッスンの予約整合性を確認できませんでした: {exc}")

    response = reservation_cancel_override.reservation_list(request)
    return _replace_html(response, _simplify_reservation_page)
