import json
from datetime import date, datetime, timedelta

from django.utils import timezone
from django.utils.deprecation import MiddlewareMixin


_CANCEL_POLICY_PATCHED = False


def _is_2026_july_preopen_general_reservation(reservation) -> bool:
    if not reservation:
        return False

    try:
        from .models import Reservation, is_preopen_cash_lesson_date
    except Exception:
        return False

    try:
        if getattr(reservation, "lesson_type", "") != Reservation.LESSON_GENERAL:
            return False
    except Exception:
        return False

    start_at = getattr(reservation, "start_at", None)
    if not start_at:
        return False

    try:
        return bool(is_preopen_cash_lesson_date(start_at))
    except Exception:
        return False


def _patch_preopen_last_cancel_policy():
    """
    2026年7月プレオープン一般レッスンだけ、最後の1名でも会員キャンセルを許可します。

    元の views.py では、通常運用として「最後の1名はキャンセル不可」にしています。
    ただし2026年7月はプレオープン期間で、チケット消費も通常と異なるため、
    この期間の一般レッスンだけ例外にします。
    """
    global _CANCEL_POLICY_PATCHED

    if _CANCEL_POLICY_PATCHED:
        return

    try:
        from . import views
    except Exception:
        return

    original_can_user_cancel_reservation = getattr(views, "_can_user_cancel_reservation", None)
    if not callable(original_can_user_cancel_reservation):
        return

    def can_user_cancel_reservation_with_preopen(user, reservation):
        if _is_2026_july_preopen_general_reservation(reservation):
            if not views._user_can_access_reservation(user, reservation):
                return False, "この予約を操作する権限がありません。"

            if views._is_reservation_canceled(reservation):
                return False, "この予約はすでにキャンセル済みです。"

            return True, ""

        return original_can_user_cancel_reservation(user, reservation)

    views._can_user_cancel_reservation = can_user_cancel_reservation_with_preopen
    _CANCEL_POLICY_PATCHED = True


def _parse_int(value, default):
    try:
        return int(value)
    except Exception:
        return default


def _month_start_end(year_value, month_value):
    month_start = date(year_value, month_value, 1)
    if month_value == 12:
        next_month = date(year_value + 1, 1, 1)
    else:
        next_month = date(year_value, month_value + 1, 1)
    return month_start, next_month


def _local_date_key(value):
    if not value:
        return ""
    try:
        if timezone.is_aware(value):
            value = timezone.localtime(value)
        return value.strftime("%Y%m%d")
    except Exception:
        return ""


def _court_display_name(court):
    if court:
        return str(court)
    return "未定"


def _first_active_court():
    try:
        from .models import Court

        return Court.objects.filter(is_active=True).order_by("id").first()
    except Exception:
        return None


def _fixed_lesson_datetimes_safely(fixed_lesson, target_date):
    if not fixed_lesson or not target_date:
        return None, None

    try:
        return fixed_lesson._build_datetimes_for_date(target_date)
    except Exception:
        pass

    try:
        start_hour = int(getattr(fixed_lesson, "start_hour", 0) or 0)
        if start_hour < 0 or start_hour > 23:
            return None, None

        start_dt = datetime.combine(target_date, datetime.min.time()).replace(hour=start_hour, minute=0)
        if timezone.is_naive(start_dt):
            start_dt = timezone.make_aware(start_dt)

        lesson_type = getattr(fixed_lesson, "lesson_type", "")
        duration_hours = 2 if lesson_type == "general" else 1
        return start_dt, start_dt + timedelta(hours=duration_hours)
    except Exception:
        return None, None


def _fixed_occurrence_dates(fixed_lesson, month_start, next_month):
    if not fixed_lesson:
        return []

    try:
        dates = list(fixed_lesson.scheduled_occurrence_dates())
        return [d for d in dates if month_start <= d < next_month]
    except Exception:
        pass

    try:
        repeat_start = getattr(fixed_lesson, "start_date", None) or month_start
        first_offset = (int(fixed_lesson.weekday) - repeat_start.weekday()) % 7
        first_date = repeat_start + timedelta(days=first_offset)
        occurrence_count = max(int(getattr(fixed_lesson, "weeks_ahead", 1) or 1), 1)
        dates = [first_date + timedelta(days=7 * index) for index in range(occurrence_count)]
        return [d for d in dates if month_start <= d < next_month]
    except Exception:
        return []


def _primary_coach_for_fixed_lesson(fixed_lesson):
    if not fixed_lesson:
        return None

    try:
        return fixed_lesson.primary_coach()
    except Exception:
        return getattr(fixed_lesson, "coach", None)


def _matching_availability_for_fixed(fixed_lesson, start_at, end_at):
    if not fixed_lesson or not start_at or not end_at:
        return None

    try:
        from .models import CoachAvailability

        primary_coach = _primary_coach_for_fixed_lesson(fixed_lesson)
        qs = CoachAvailability.objects.select_related("court").filter(
            coach=primary_coach,
            lesson_type=getattr(fixed_lesson, "lesson_type", ""),
            start_at=start_at,
            end_at=end_at,
        )
        if getattr(fixed_lesson, "court_id", None):
            qs = qs.filter(court=fixed_lesson.court)
        return qs.order_by("id").first()
    except Exception:
        return None


def _build_lesson_calendar_court_map(request):
    today = timezone.localdate()
    target_year = _parse_int(request.GET.get("year"), today.year)
    target_month = _parse_int(request.GET.get("month"), today.month)

    if target_month < 1 or target_month > 12:
        target_month = today.month

    try:
        month_start, next_month = _month_start_end(target_year, target_month)
    except Exception:
        month_start, next_month = _month_start_end(today.year, today.month)

    court_map = {}

    try:
        from .models import CoachAvailability, FixedLesson

        default_court = _first_active_court()

        fixed_lessons = (
            FixedLesson.objects.filter(is_active=True)
            .select_related("coach", "coach_2", "coach_3", "court")
            .order_by("weekday", "start_hour", "id")
        )

        for fixed_lesson in fixed_lessons:
            for target_date in _fixed_occurrence_dates(fixed_lesson, month_start, next_month):
                start_at, end_at = _fixed_lesson_datetimes_safely(fixed_lesson, target_date)
                if not start_at or not end_at:
                    continue

                matching_availability = _matching_availability_for_fixed(fixed_lesson, start_at, end_at)
                court = getattr(matching_availability, "court", None) or getattr(fixed_lesson, "court", None) or default_court
                key = f"fixed-{fixed_lesson.pk}-{target_date:%Y%m%d}"
                court_map[key] = _court_display_name(court)

        availability_qs = (
            CoachAvailability.objects.filter(
                start_at__date__gte=month_start,
                start_at__date__lt=next_month,
            )
            .select_related("court")
            .order_by("start_at", "id")
        )

        for availability in availability_qs:
            key = str(availability.pk)
            court_map[key] = _court_display_name(getattr(availability, "court", None))

    except Exception:
        return {}

    return court_map


def _inject_lesson_calendar_notice_and_courts(request, html):
    if not request.path.startswith("/lesson-calendar/"):
        return html

    if "lesson-calendar-court-notice-script" in html:
        return html

    court_map = _build_lesson_calendar_court_map(request)
    court_map_json = json.dumps(court_map, ensure_ascii=False)

    injection = f"""
<style id="lesson-calendar-court-notice-style">
  .court-weather-notice{{
    margin-top:12px;
    border-color:#0ea5e9!important;
    background:#f0f9ff!important;
    color:#075985!important;
  }}
  .court-weather-notice .ticket-notice-icon{{
    background:#0ea5e9!important;
  }}
  .event-court{{
    margin-top:2px;
    color:#334155;
    font-size:9px;
    line-height:1.08;
    font-weight:950;
  }}
  .schedule-court{{
    display:inline-flex;
    align-items:center;
    justify-content:center;
    padding:3px 9px;
    border-radius:999px;
    background:#eff6ff;
    color:#0750b8;
    border:1px solid #bfdbfe;
    font-size:12px;
    line-height:1.2;
    font-weight:1000;
    white-space:nowrap;
  }}
  @media (max-width:768px){{
    .event-court{{
      margin-top:1px;
      font-size:6.4px;
      line-height:1.02;
      letter-spacing:-.08em;
    }}
    .schedule-court{{
      padding:2px 7px;
      font-size:10px;
    }}
  }}
</style>
<script id="lesson-calendar-court-notice-script">
(function () {{
  const courtByKey = {court_map_json};

  function ready(callback) {{
    if (document.readyState === "loading") {{
      document.addEventListener("DOMContentLoaded", callback);
    }} else {{
      callback();
    }}
  }}

  function keyFromUrl(rawUrl) {{
    if (!rawUrl) return "";
    try {{
      const url = new URL(rawUrl, window.location.origin);
      const params = url.searchParams;
      const fixedLessonId = params.get("fixed_lesson_id");
      const lessonDate = params.get("lesson_date");
      if (fixedLessonId && lessonDate) {{
        return "fixed-" + fixedLessonId + "-" + lessonDate.replaceAll("-", "");
      }}
      const availabilityId = params.get("availability_id");
      if (availabilityId) return availabilityId;
    }} catch (error) {{
      return "";
    }}
    return "";
  }}

  function keyFromEvent(element) {{
    return keyFromUrl(element.getAttribute("data-member-list-url") || element.getAttribute("href") || "");
  }}

  function addNotice() {{
    const monthNav = document.querySelector(".calendar-month-nav");
    if (!monthNav || document.querySelector(".court-weather-notice")) return;

    const notice = document.createElement("div");
    notice.className = "ticket-notice court-weather-notice";
    notice.innerHTML =
      '<span class="ticket-notice-icon">i</span>' +
      '<div>' +
      '<p class="ticket-notice-title">雨天中止・コートについて</p>' +
      '<p class="ticket-notice-text">雨天中止の場合は、レッスン開始1時間前までを目安にご連絡します。コートは西猪名公園または尼崎記念公園となる可能性があります。各レッスン欄のコート表示をご確認ください。</p>' +
      '</div>';

    monthNav.parentNode.insertBefore(notice, monthNav);
  }}

  function addCourtToCalendarEvents() {{
    document.querySelectorAll(".calendar-event").forEach(function (eventElement) {{
      if (eventElement.querySelector(".event-court")) return;

      const key = keyFromEvent(eventElement);
      const courtName = courtByKey[key];
      if (!courtName) return;

      const courtElement = document.createElement("div");
      courtElement.className = "event-court";
      courtElement.textContent = "コート：" + courtName;

      const timeElement = eventElement.querySelector(".event-time");
      if (timeElement && timeElement.parentNode) {{
        timeElement.insertAdjacentElement("afterend", courtElement);
      }} else {{
        eventElement.appendChild(courtElement);
      }}
    }});
  }}

  function addCourtToScheduleRows() {{
    document.querySelectorAll('.schedule-row[id^="lesson-"]').forEach(function (row) {{
      if (row.querySelector(".schedule-court")) return;

      const key = row.id.replace(/^lesson-/, "");
      const courtName = courtByKey[key];
      if (!courtName) return;

      const detail = row.querySelector(".schedule-detail");
      if (!detail) return;

      const courtElement = document.createElement("span");
      courtElement.className = "schedule-court";
      courtElement.textContent = "コート：" + courtName;

      const firstSpan = detail.querySelector("span");
      if (firstSpan) {{
        firstSpan.insertAdjacentElement("afterend", courtElement);
      }} else {{
        detail.insertBefore(courtElement, detail.firstChild);
      }}
    }});
  }}

  ready(function () {{
    addNotice();
    addCourtToCalendarEvents();
    addCourtToScheduleRows();
  }});
}})();
</script>
"""

    if "</body>" in html:
        return html.replace("</body>", injection + "\n</body>", 1)

    return html + injection


class AdminDashboardMenuMiddleware(MiddlewareMixin):
    """
    コーチ・業務委託コーチ・admin 用の共通メニューに、かんたん管理への導線を追加します。

    併せて、2026年7月プレオープン一般レッスンの「最後の1名キャンセル不可」例外、
    レッスンカレンダーへの雨天・コート案内、各レッスンのコート表示を適用します。
    """

    shortcut_marker = 'href="/admin-dashboard/"'
    daily_group_marker = '<h2 class="coach-menu-group-title">日常業務</h2>\n                <div class="coach-tabs">'

    def process_request(self, request):
        _patch_preopen_last_cancel_policy()
        return None

    def process_response(self, request, response):
        user = getattr(request, "user", None)

        if getattr(response, "streaming", False):
            return response

        content_type = response.get("Content-Type", "")
        if "text/html" not in content_type:
            return response

        try:
            html = response.content.decode(response.charset or "utf-8")
        except Exception:
            return response

        html = _inject_lesson_calendar_notice_and_courts(request, html)

        if user and getattr(user, "is_authenticated", False):
            is_coach_menu_user = (
                getattr(user, "role", "") in ("coach", "contractor_coach")
                or bool(getattr(user, "is_staff", False))
                or bool(getattr(user, "is_superuser", False))
            )

            if is_coach_menu_user and self.shortcut_marker not in html and self.daily_group_marker in html:
                active_class = " active" if request.path.startswith("/admin-dashboard/") else ""
                shortcut_html = (
                    self.daily_group_marker
                    + "\n"
                    + f'                  <a href="/admin-dashboard/" class="coach-tab{active_class}">かんたん管理</a>'
                )
                html = html.replace(self.daily_group_marker, shortcut_html, 1)

        encoded = html.encode(response.charset or "utf-8")
        response.content = encoded
        response["Content-Length"] = str(len(encoded))
        return response
