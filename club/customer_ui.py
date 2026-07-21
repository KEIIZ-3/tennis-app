import re

from django.contrib.auth.decorators import login_required

from . import reservation_cancel_override, views


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


@login_required
def tickets_view(request):
    response = views.tickets_view(request)
    return _replace_html(response, _improve_ticket_page)


@login_required
def reservation_list(request):
    response = reservation_cancel_override.reservation_list(request)
    return _replace_html(response, _simplify_reservation_page)
