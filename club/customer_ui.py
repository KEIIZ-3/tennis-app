import html as html_module
import re
from urllib.parse import parse_qs, urlparse

from django.contrib.auth.decorators import login_required
from django.http import HttpResponseForbidden
from django.utils import timezone

from . import views
from .fixed_occurrence_participants import active_count_map_for_month


CALENDAR_ANCHOR_PATTERN = re.compile(
    r'(<a\b[^>]*data-member-list-url="(?P<url>[^"]+)"[^>]*>)(?P<body>.*?)(</a>)',
    re.DOTALL,
)
CALENDAR_COUNT_PATTERN = re.compile(
    r'(<div class="event-meta">)\s*\d+\s*/\s*(?P<capacity>\d+)名(</div>)'
)


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


def _calendar_target_month(request):
    today = timezone.localdate()
    try:
        year = int(request.GET.get("year") or today.year)
    except (TypeError, ValueError):
        year = today.year
    try:
        month = int(request.GET.get("month") or today.month)
    except (TypeError, ValueError):
        month = today.month
    if month < 1 or month > 12:
        month = today.month
    return year, month


def _replace_fixed_occurrence_counts(document, count_map):
    """固定開催回カードの人数を、その開催回に紐づく有効予約数へ統一する。"""

    def replace_anchor(match):
        raw_url = html_module.unescape(match.group("url"))
        query = parse_qs(urlparse(raw_url).query)
        fixed_lesson_id = (query.get("fixed_lesson_id") or [""])[0]
        lesson_date = (query.get("lesson_date") or [""])[0]
        count = count_map.get((str(fixed_lesson_id), lesson_date))
        if count is None:
            return match.group(0)

        body = match.group("body")
        body = CALENDAR_COUNT_PATTERN.sub(
            lambda count_match: (
                f'{count_match.group(1)}{int(count)}/'
                f'{count_match.group("capacity")}名{count_match.group(3)}'
            ),
            body,
            count=1,
        )
        return match.group(1) + body + match.group(4)

    return CALENDAR_ANCHOR_PATTERN.sub(replace_anchor, document)


def _improve_lesson_calendar(html, count_map=None):
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
    if count_map:
        html = _replace_fixed_occurrence_counts(html, count_map)
    return html


def lesson_calendar_view(request):
    year, month = _calendar_target_month(request)
    count_map = active_count_map_for_month(year, month)
    response = views.lesson_calendar_view(request)
    response = _replace_html(
        response,
        lambda document: _improve_lesson_calendar(document, count_map=count_map),
    )
    response["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response["Pragma"] = "no-cache"
    return response


@login_required
def lesson_reservation_confirm(request):
    return views.lesson_reservation_confirm(request)


@login_required
def tickets_view(request):
    response = views.tickets_view(request)
    return _replace_html(response, _improve_ticket_page)


@login_required
def reservation_list(request):
    if getattr(request.user, "role", "") != "member":
        return HttpResponseForbidden("予約確認は会員専用です。")

    return views.reservation_list(request)
