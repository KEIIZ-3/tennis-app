import html
import re
from urllib.parse import parse_qs, urlparse

from django.utils import timezone

from . import customer_ui
from .fixed_occurrence_participants import active_count_map_for_month


ANCHOR_PATTERN = re.compile(
    r'(<a\b[^>]*data-member-list-url="(?P<url>[^"]+)"[^>]*>)(?P<body>.*?)(</a>)',
    re.DOTALL,
)
COUNT_PATTERN = re.compile(
    r'(<div class="event-meta">)\s*\d+\s*/\s*(?P<capacity>\d+)名(</div>)'
)


def _target_month(request):
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
    def replace_anchor(match):
        raw_url = html.unescape(match.group("url"))
        query = parse_qs(urlparse(raw_url).query)
        fixed_lesson_id = (query.get("fixed_lesson_id") or [""])[0]
        lesson_date = (query.get("lesson_date") or [""])[0]
        count = count_map.get((str(fixed_lesson_id), lesson_date))
        if count is None:
            return match.group(0)

        body = match.group("body")
        body = COUNT_PATTERN.sub(
            lambda count_match: (
                f'{count_match.group(1)}{int(count)}/'
                f'{count_match.group("capacity")}名{count_match.group(3)}'
            ),
            body,
            count=1,
        )
        return match.group(1) + body + match.group(4)

    return ANCHOR_PATTERN.sub(replace_anchor, document)


def lesson_calendar_view(request):
    response = customer_ui.lesson_calendar_view(request)
    content_type = response.get("Content-Type", "")
    if response.status_code != 200 or "text/html" not in content_type:
        return response

    year, month = _target_month(request)
    count_map = active_count_map_for_month(year, month)
    charset = response.charset or "utf-8"
    document = response.content.decode(charset)
    updated = _replace_fixed_occurrence_counts(document, count_map)
    response.content = updated.encode(charset)
    response["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response["Pragma"] = "no-cache"
    if response.has_header("Content-Length"):
        response["Content-Length"] = str(len(response.content))
    return response
