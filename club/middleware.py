import json
from contextvars import ContextVar
from datetime import date, datetime, timedelta

from django.utils import timezone
from django.utils.deprecation import MiddlewareMixin


_preopen_level_free_request = ContextVar(
    "preopen_level_free_request",
    default=False,
)


def preopen_level_free_enabled():
    return bool(_preopen_level_free_request.get())


def _request_is_preopen_july(request):
    values = [
        request.GET.get("lesson_date"),
        request.POST.get("lesson_date"),
        request.GET.get("date"),
        request.POST.get("date"),
        request.GET.get("start"),
        request.POST.get("start"),
    ]
    for value in values:
        text = str(value or "").strip()
        if text.startswith(("2026-07", "2026/07", "2026/7")):
            return True
    try:
        year = request.GET.get("year") or request.POST.get("year")
        month = request.GET.get("month") or request.POST.get("month")
        return int(year or 0) == 2026 and int(month or 0) == 7
    except (TypeError, ValueError):
        return False


class PreopenLevelFreeMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        token = _preopen_level_free_request.set(
            _request_is_preopen_july(request)
        )
        try:
            return self.get_response(request)
        finally:
            _preopen_level_free_request.reset(token)


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


def _nth_weekday(year_value, month_value, weekday_value, nth_value):
    target = date(year_value, month_value, 1)
    offset = (weekday_value - target.weekday()) % 7
    return target + timedelta(days=offset + (nth_value - 1) * 7)


def _vernal_equinox_day(year_value):
    # 2099年までの日本の春分日近似式。現行運用上のレッスンカレンダー表示用途。
    return int(20.8431 + 0.242194 * (year_value - 1980) - int((year_value - 1980) / 4))


def _autumnal_equinox_day(year_value):
    # 2099年までの日本の秋分日近似式。現行運用上のレッスンカレンダー表示用途。
    return int(23.2488 + 0.242194 * (year_value - 1980) - int((year_value - 1980) / 4))


def _base_japanese_holidays(year_value):
    holidays = {
        date(year_value, 1, 1): "元日",
        _nth_weekday(year_value, 1, 0, 2): "成人の日",
        date(year_value, 2, 11): "建国記念の日",
        date(year_value, 2, 23): "天皇誕生日",
        date(year_value, 3, _vernal_equinox_day(year_value)): "春分の日",
        date(year_value, 4, 29): "昭和の日",
        date(year_value, 5, 3): "憲法記念日",
        date(year_value, 5, 4): "みどりの日",
        date(year_value, 5, 5): "こどもの日",
        date(year_value, 8, 11): "山の日",
        _nth_weekday(year_value, 9, 0, 3): "敬老の日",
        date(year_value, 9, _autumnal_equinox_day(year_value)): "秋分の日",
        _nth_weekday(year_value, 10, 0, 2): "スポーツの日",
        date(year_value, 11, 3): "文化の日",
        date(year_value, 11, 23): "勤労感謝の日",
    }

    # 海の日：7月第3月曜日
    holidays[_nth_weekday(year_value, 7, 0, 3)] = "海の日"

    return holidays


def _lesson_calendar_special_closed_days_for_year(year_value):
    """
    レッスンカレンダー上で、祝日以外に休業期間として表示したい日を定義します。
    2026/8/11〜2026/8/14 はお盆休みとして表示します。
    """
    try:
        year_number = int(year_value)
    except Exception:
        return {}

    if year_number != 2026:
        return {}

    return {
        date(2026, 8, 11): "お盆休み・休講",
        date(2026, 8, 12): "お盆休み・休講",
        date(2026, 8, 13): "お盆休み・休講",
        date(2026, 8, 14): "お盆休み・休講",
    }


def _japanese_holidays_for_year(year_value):
    holidays = dict(_base_japanese_holidays(year_value))

    # 国民の休日：祝日と祝日に挟まれた平日
    cursor = date(year_value, 1, 2)
    year_end = date(year_value, 12, 30)
    while cursor <= year_end:
        if cursor not in holidays:
            previous_day = cursor - timedelta(days=1)
            next_day = cursor + timedelta(days=1)
            if previous_day in holidays and next_day in holidays:
                holidays[cursor] = "国民の休日"
        cursor += timedelta(days=1)

    # 振替休日：日曜に祝日が当たる場合、以後最初の平日を休日にする
    for holiday_date, holiday_name in sorted(list(holidays.items())):
        if holiday_date.weekday() != 6:
            continue

        substitute_date = holiday_date + timedelta(days=1)
        while substitute_date in holidays:
            substitute_date += timedelta(days=1)

        if substitute_date.year == year_value:
            holidays[substitute_date] = f"{holiday_name} 振替休日"

    return dict(sorted(holidays.items()))


def _japanese_holiday_map_for_month(year_value, month_value):
    try:
        month_start, next_month = _month_start_end(year_value, month_value)
    except Exception:
        today = timezone.localdate()
        month_start, next_month = _month_start_end(today.year, today.month)

    holidays = {}
    for target_year in {month_start.year, next_month.year}:
        holidays.update(_japanese_holidays_for_year(target_year))
        holidays.update(_lesson_calendar_special_closed_days_for_year(target_year))

    return {
        target_date.isoformat(): holiday_name
        for target_date, holiday_name in holidays.items()
        if month_start <= target_date < next_month
    }


def _court_display_name(court):
    if not court:
        return "未定"

    court_name = str(court)

    try:
        court_type_label = court.get_court_type_display()
    except Exception:
        court_type_label = ""

    court_type_label = (court_type_label or "").strip()
    court_name = (court_name or "").strip()

    if not court_type_label:
        return court_name or "未定"

    if court_name and court_type_label in court_name:
        return court_name

    if court_name:
        return f"{court_type_label}：{court_name}"

    return court_type_label


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



def _build_lesson_calendar_capacity_map(request):
    """
    レッスンカレンダー上の人数表示を、固定レッスンの現在設定に合わせます。

    固定レッスンの担当コーチ人数を 2→1 に戻した場合、
    古い CoachAvailability に capacity=12 が残っていても、
    fixed_lesson_id + lesson_date のキーでは FixedLesson.effective_capacity() を優先します。
    """
    today = timezone.localdate()
    target_year = _parse_int(request.GET.get("year"), today.year)
    target_month = _parse_int(request.GET.get("month"), today.month)

    if target_month < 1 or target_month > 12:
        target_month = today.month

    try:
        month_start, next_month = _month_start_end(target_year, target_month)
    except Exception:
        month_start, next_month = _month_start_end(today.year, today.month)

    capacity_map = {}

    try:
        from .models import CoachAvailability, FixedLesson

        fixed_lessons = (
            FixedLesson.objects.filter(is_active=True)
            .select_related("coach", "coach_2", "coach_3", "court")
            .prefetch_related("members")
            .order_by("weekday", "start_hour", "id")
        )

        for fixed_lesson in fixed_lessons:
            try:
                fixed_capacity = int(fixed_lesson.effective_capacity())
            except Exception:
                fixed_capacity = int(getattr(fixed_lesson, "capacity", 0) or 0)

            try:
                fixed_member_count = fixed_lesson.members.count()
            except Exception:
                fixed_member_count = 0

            display_capacity = max(fixed_capacity, fixed_member_count, 1)

            for target_date in _fixed_occurrence_dates(fixed_lesson, month_start, next_month):
                key = f"fixed-{fixed_lesson.pk}-{target_date:%Y%m%d}"
                capacity_map[key] = display_capacity

        availability_qs = (
            CoachAvailability.objects.filter(
                start_at__date__gte=month_start,
                start_at__date__lt=next_month,
            )
            .select_related("court")
            .order_by("start_at", "id")
        )

        for availability in availability_qs:
            try:
                availability_capacity = int(availability.effective_capacity())
            except Exception:
                availability_capacity = int(getattr(availability, "capacity", 0) or 0)

            key = str(availability.pk)
            capacity_map[key] = max(availability_capacity, 1)

    except Exception:
        return {}

    return capacity_map


def _calendar_target_year_month(request):
    today = timezone.localdate()
    target_year = _parse_int(request.GET.get("year"), today.year)
    target_month = _parse_int(request.GET.get("month"), today.month)

    if target_month < 1 or target_month > 12:
        target_month = today.month

    return target_year, target_month




def _inject_lesson_calendar_notice_courts_and_holidays(request, html):
    if not request.path.startswith("/lesson-calendar/"):
        return html

    if "lesson-calendar-court-notice-script" in html:
        return html

    target_year, target_month = _calendar_target_year_month(request)
    court_map = _build_lesson_calendar_court_map(request)
    capacity_map = _build_lesson_calendar_capacity_map(request)
    holiday_map = _japanese_holiday_map_for_month(target_year, target_month)

    court_map_json = json.dumps(court_map, ensure_ascii=False)
    capacity_map_json = json.dumps(capacity_map, ensure_ascii=False)
    holiday_map_json = json.dumps(holiday_map, ensure_ascii=False)
    is_2026_july = target_year == 2026 and target_month == 7
    is_2026_obon = target_year == 2026 and target_month == 8

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
  .court-entry-deadline-note{{
    margin-top:6px;
    color:#9a3412;
    font-weight:1000;
  }}
  .obon-closed-note{{
    margin-top:8px;
    padding:8px 10px;
    border-radius:12px;
    border:1px solid #fecaca;
    background:#fff1f2;
    color:#991b1b;
    font-weight:1000;
  }}
  .monthly-calendar td.is-japanese-holiday{{
    background:#fff1f2!important;
  }}
  .monthly-calendar td.is-japanese-holiday .day-number{{
    color:#be123c!important;
  }}
  .monthly-calendar td.is-japanese-holiday.day-cell-past{{
    background:#fce7f3!important;
  }}
  .monthly-calendar td.is-obon-holiday{{
    background:repeating-linear-gradient(135deg,#fff1f2 0,#fff1f2 8px,#ffe4e6 8px,#ffe4e6 16px)!important;
    border:2px solid #fb7185!important;
  }}
  .monthly-calendar td.is-obon-holiday .day-number{{
    color:#991b1b!important;
    font-weight:1000!important;
  }}
  .monthly-calendar td.is-obon-holiday .holiday-name{{
    display:flex;
    width:100%;
    margin:5px 0 4px;
    padding:5px 4px;
    border-radius:8px;
    background:#be123c;
    color:#fff;
    border:1px solid #9f1239;
    box-shadow:0 4px 10px rgba(190,18,60,.22);
    font-size:11px;
    line-height:1.15;
    white-space:normal;
  }}
  .holiday-name{{
    display:inline-flex;
    align-items:center;
    justify-content:center;
    max-width:100%;
    margin:3px 0 1px;
    padding:2px 6px;
    border-radius:999px;
    background:#ffe4e6;
    color:#be123c;
    border:1px solid #fecdd3;
    font-size:10px;
    line-height:1.1;
    font-weight:1000;
    white-space:nowrap;
    overflow:hidden;
    text-overflow:ellipsis;
  }}
  .event-court{{
    margin-top:2px;
    color:#334155;
    font-size:8.5px;
    line-height:1.08;
    font-weight:950;
    max-width:100%;
    word-break:keep-all;
    overflow:hidden;
    text-overflow:ellipsis;
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
    white-space:normal;
  }}
  @media (max-width:768px){{
    .holiday-name{{
      display:block;
      margin-top:2px;
      padding:1px 2px;
      border-radius:4px;
      font-size:5.8px;
      line-height:1.02;
      letter-spacing:-.10em;
      white-space:normal;
      overflow:visible;
      text-overflow:clip;
    }}
    .monthly-calendar td.is-obon-holiday .holiday-name{{
      display:block;
      margin-top:3px;
      padding:3px 2px;
      border-radius:6px;
      font-size:7.4px;
      line-height:1.05;
      letter-spacing:-.08em;
      white-space:normal;
      overflow:visible;
      text-overflow:clip;
    }}
    .event-court{{
      margin-top:1px;
      font-size:5.8px;
      line-height:1.02;
      letter-spacing:-.10em;
      white-space:normal;
      overflow:visible;
      text-overflow:clip;
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
  const capacityByKey = {capacity_map_json};
  const holidayByDate = {holiday_map_json};
  const targetYear = {int(target_year)};
  const targetMonth = {int(target_month)};
  const isJulyPreopen2026 = {str(is_2026_july).lower()};
  const isObonClosedMonth2026 = {str(is_2026_obon).lower()};

  function ready(callback) {{
    if (document.readyState === "loading") {{
      document.addEventListener("DOMContentLoaded", callback);
    }} else {{
      callback();
    }}
  }}

  function zeroPad(value) {{
    return String(value).padStart(2, "0");
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

  function memberListUrlFromEvent(element) {{
    const url = element.getAttribute("data-member-list-url") || "";
    if (!url) return "";
    return url;
  }}

  function keyFromEvent(element) {{
    return keyFromUrl(element.getAttribute("data-member-list-url") || element.getAttribute("href") || "");
  }}

  function addNotice() {{
    const monthNav = document.querySelector(".calendar-month-nav");
    if (!monthNav || document.querySelector(".court-weather-notice")) return;

    const julyDeadlineText = isJulyPreopen2026
      ? '<p class="ticket-notice-text court-entry-deadline-note">2026年7月分はコートキャンセル期限が1週間前のため、開催日の1週間前までにエントリーをお願いします。</p>'
      : '';

    const obonClosedText = isObonClosedMonth2026
      ? '<p class="ticket-notice-text obon-closed-note">お盆休み：2026/8/11（火）〜8/14（金）はレッスン休講予定です。カレンダー内の赤い表示をご確認ください。</p>'
      : '';

    const notice = document.createElement("div");
    notice.className = "ticket-notice court-weather-notice";
    notice.innerHTML =
      '<span class="ticket-notice-icon">i</span>' +
      '<div>' +
      '<p class="ticket-notice-title">雨天中止・コートについて</p>' +
      '<p class="ticket-notice-text">雨天中止の場合は、レッスン開始1時間前までを目安にご連絡します。コートは西猪名公園または尼崎記念公園となる可能性があります。各レッスン欄のコート表示をご確認ください。</p>' +
      julyDeadlineText +
      obonClosedText +
      '</div>';

    monthNav.parentNode.insertBefore(notice, monthNav);
  }}

  function addHolidayBackgrounds() {{
    document.querySelectorAll(".monthly-calendar td").forEach(function (cell) {{
      if (cell.classList.contains("day-cell-muted")) return;
      if (cell.querySelector(".holiday-name")) return;

      const dayNumberElement = cell.querySelector(".day-number");
      if (!dayNumberElement) return;

      const dayNumber = parseInt((dayNumberElement.textContent || "").trim(), 10);
      if (!dayNumber) return;

      const dateKey = String(targetYear) + "-" + zeroPad(targetMonth) + "-" + zeroPad(dayNumber);
      const holidayName = holidayByDate[dateKey];
      if (!holidayName) return;

      cell.classList.add("is-japanese-holiday");
      if (String(holidayName).indexOf("お盆休み") !== -1) {{
        cell.classList.add("is-obon-holiday");
      }}

      const holidayElement = document.createElement("div");
      holidayElement.className = "holiday-name";
      holidayElement.textContent = holidayName;
      dayNumberElement.insertAdjacentElement("afterend", holidayElement);
    }});
  }}

  function replaceCapacityTextInElement(element, capacity) {{
    if (!element || !capacity) return;
    // 日付の「7/17」などを壊さないため、「4/6名」のように末尾に「名」がある人数表示だけを置換します。

    const walker = document.createTreeWalker(element, NodeFilter.SHOW_TEXT);
    const textNodes = [];
    while (walker.nextNode()) {{
      textNodes.push(walker.currentNode);
    }}

    textNodes.forEach(function (node) {{
      const before = node.nodeValue || "";
      const after = before.replace(/(\\d+)\\s*\\/\\s*\\d+名/g, function (_match, count) {{
        return count + "/" + capacity + "名";
      }});
      if (after !== before) {{
        node.nodeValue = after;
      }}
    }});
  }}

  function normalizeCapacityDisplays() {{
    document.querySelectorAll(".calendar-event").forEach(function (eventElement) {{
      const key = keyFromEvent(eventElement);
      const capacity = capacityByKey[key];
      if (!capacity) return;
      replaceCapacityTextInElement(eventElement, capacity);

      const title = eventElement.getAttribute("title") || "";
      if (title) {{
        eventElement.setAttribute("title", title.replace(/(\\d+)\\s*\\/\\s*\\d+名/g, function (_match, count) {{
          return count + "/" + capacity + "名";
        }}));
      }}
    }});

    document.querySelectorAll('.schedule-row[id^="lesson-"]').forEach(function (row) {{
      const key = row.id.replace(/^lesson-/, "");
      const capacity = capacityByKey[key];
      if (!capacity) return;
      replaceCapacityTextInElement(row, capacity);
    }});
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

  function routeJulyCardsToMemberList() {{
    if (!isJulyPreopen2026) return;

    document.querySelectorAll(".calendar-event").forEach(function (eventElement) {{
      const memberListUrl = memberListUrlFromEvent(eventElement);
      if (!memberListUrl) return;

      eventElement.setAttribute("href", memberListUrl);
      eventElement.setAttribute("aria-label", "参加状況を確認する");
      eventElement.setAttribute("title", "参加状況を確認する");
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
    addHolidayBackgrounds();
    normalizeCapacityDisplays();
    addCourtToCalendarEvents();
    routeJulyCardsToMemberList();
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

    併せて、コート種別の管理サイト選択肢補正、
    固定レッスン等の対象レベル「全レベル」選択肢補正、
    2026年7月プレオープン一般レッスンの「最後の1名キャンセル不可」例外、
    レッスンカレンダーへの雨天・コート案内、各レッスンのコート種別・コート名表示、
    日本の祝日背景色表示、2026年7月分の1週間前エントリー案内、
    2026年7月分の顧客向け参加状況表示、
    2026年8月のお盆休み強調表示、
    固定レッスンの担当コーチ変更・定員再同期の安全運用、
    固定レッスン由来データの実同期、
    レッスンカレンダーの定員表示補正を適用します。
    """

    shortcut_marker = 'href="/admin-dashboard/"'
    daily_group_marker = '<h2 class="coach-menu-group-title">日常業務</h2>\n                <div class="coach-tabs">'

    def process_response(self, request, response):
        if getattr(response, "streaming", False):
            return response

        content_type = response.get("Content-Type", "")
        if "text/html" not in content_type:
            return response

        try:
            html = response.content.decode(response.charset or "utf-8")
        except Exception:
            return response

        html = _inject_lesson_calendar_notice_courts_and_holidays(request, html)
        encoded = html.encode(response.charset or "utf-8")
        response.content = encoded
        response["Content-Length"] = str(len(encoded))
        return response
