from datetime import datetime, time
from decimal import Decimal, ROUND_HALF_UP

import jpholiday
from django.utils import timezone


AMAGASAKI_RATE_URL = "https://www.aspf.or.jp/park/tennis.html#gsc.tab=0"
SONO_RATE_URL = "https://www.hyogo-park.or.jp/nishiina/contents/sisetsu/area_sports.html"


def _local(value):
    if value and timezone.is_aware(value):
        return timezone.localtime(value)
    return value


def _money(value):
    return int(Decimal(str(value)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _court_kind(court):
    if not court:
        return ""

    court_type = str(getattr(court, "court_type", "") or "").strip().lower()
    court_name = str(getattr(court, "name", "") or str(court)).strip()

    if court_type == "amagasaki" or ("尼崎" in court_name and "記念" in court_name):
        return "amagasaki"
    if court_type == "sono" or "西猪名" in court_name:
        return "sono"
    return ""


def _overlap_minutes(start_at, end_at, range_start, range_end):
    overlap_start = max(start_at, range_start)
    overlap_end = min(end_at, range_end)
    if overlap_end <= overlap_start:
        return 0
    return max(int((overlap_end - overlap_start).total_seconds() // 60), 0)


def calculate_court_fee(court, start_at, end_at, court_count=1):
    start_at = _local(start_at)
    end_at = _local(end_at)
    kind = _court_kind(court)

    if not kind or not start_at or not end_at or end_at <= start_at:
        return None

    court_count = max(int(court_count or 1), 1)
    duration_minutes = max(int((end_at - start_at).total_seconds() // 60), 0)
    duration_hours = Decimal(duration_minutes) / Decimal(60)
    target_date = start_at.date()
    is_holiday = bool(jpholiday.is_holiday(target_date))
    is_saturday = target_date.weekday() == 5
    is_sunday = target_date.weekday() == 6

    lighting_start = datetime.combine(target_date, time(19, 0))
    lighting_end = datetime.combine(target_date, time(21, 0))
    if timezone.is_aware(start_at):
        lighting_start = timezone.make_aware(lighting_start, timezone.get_current_timezone())
        lighting_end = timezone.make_aware(lighting_end, timezone.get_current_timezone())

    lighting_minutes = _overlap_minutes(
        start_at,
        end_at,
        lighting_start,
        lighting_end,
    )

    if kind == "amagasaki":
        premium = is_sunday or is_holiday
        hourly_rate = 1080 if premium else 900
        lighting_hourly_rate = 200
        facility_label = "尼崎記念公園"
        rate_label = "日曜・祝日" if premium else "平日・土曜"
        rate_url = AMAGASAKI_RATE_URL
        rate_link_label = "尼崎市スポーツ振興事業団の料金表を確認"
    else:
        premium = is_saturday or is_sunday or is_holiday
        hourly_rate = 1200 if premium else 900
        lighting_hourly_rate = 400
        facility_label = "西猪名公園"
        rate_label = "土曜・日曜・祝日" if premium else "平日"
        rate_url = SONO_RATE_URL
        rate_link_label = "西猪名公園の公式料金表を確認"

    base_amount = _money(Decimal(hourly_rate) * duration_hours * court_count)
    lighting_amount = _money(
        Decimal(lighting_hourly_rate)
        * (Decimal(lighting_minutes) / Decimal(60))
        * court_count
    )
    total = base_amount + lighting_amount

    return {
        "facility": kind,
        "facility_label": facility_label,
        "rate_label": rate_label,
        "hourly_rate": hourly_rate,
        "lighting_hourly_rate": lighting_hourly_rate,
        "duration_minutes": duration_minutes,
        "duration_hours": duration_hours.normalize(),
        "court_count": court_count,
        "base_amount": base_amount,
        "lighting_minutes": lighting_minutes,
        "lighting_amount": lighting_amount,
        "total": total,
        "rate_url": rate_url,
        "rate_link_label": rate_link_label,
    }


def calculate_availability_court_fee(availability):
    if not availability:
        return None
    return calculate_court_fee(
        getattr(availability, "court", None),
        getattr(availability, "start_at", None),
        getattr(availability, "end_at", None),
        getattr(availability, "court_count", 1),
    )
