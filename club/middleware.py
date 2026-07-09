import json
from datetime import date, datetime, timedelta

from django.db import transaction
from django.utils import timezone
from django.utils.deprecation import MiddlewareMixin


_CANCEL_POLICY_PATCHED = False
_COURT_TYPE_POLICY_PATCHED = False
_LEVEL_ALL_POLICY_PATCHED = False
_AVAILABILITY_SAVE_POLICY_PATCHED = False
_FIXED_LESSON_SYNC_POLICY_PATCHED = False


def _patch_court_type_choices():
    """
    管理サイトの「コート種別」プルダウンを運用名に合わせます。

    既存DB値:
    - sono: 西猪名公園
    - other: その他

    追加DB値:
    - amagasaki: 尼崎記念公園

    Courtモデル本体の巨大置換を避けるため、リクエスト処理前に
    モデルフィールドのchoicesを補正し、管理サイトのフォーム生成・保存検証に反映します。
    """
    global _COURT_TYPE_POLICY_PATCHED

    if _COURT_TYPE_POLICY_PATCHED:
        return

    try:
        from .models import Court
    except Exception:
        return

    court_type_choices = (
        ("sono", "西猪名公園"),
        ("amagasaki", "尼崎記念公園"),
        ("other", "その他"),
    )

    try:
        Court.COURT_SONO = "sono"
        Court.COURT_AMAGASAKI = "amagasaki"
        Court.COURT_OTHER = "other"
        Court.COURT_TYPE_CHOICES = court_type_choices

        field = Court._meta.get_field("court_type")
        field.choices = court_type_choices
        field.default = "sono"

        _COURT_TYPE_POLICY_PATCHED = True
    except Exception:
        return



def _patch_all_level_choices():
    """
    固定レッスン・コーチスケジュール・予約の対象レベルに「全レベル」を追加します。

    DB上は target_level / target_level_2 に "all" を保存します。
    「全レベル」は会員レベル判定を通すため、予約時は誰でも予約可能として扱います。
    会員本人の member_level には追加せず、レッスン側の対象レベルだけに適用します。
    """
    global _LEVEL_ALL_POLICY_PATCHED

    if _LEVEL_ALL_POLICY_PATCHED:
        return

    try:
        from .models import CoachAvailability, FixedLesson, Reservation, User
    except Exception:
        return

    level_all_value = "all"
    level_all_label = "全レベル"

    base_level_choices = tuple(User.LEVEL_CHOICES)
    target_level_choices = ((level_all_value, level_all_label),) + base_level_choices
    optional_target_level_choices = (("", "指定なし"),) + target_level_choices

    try:
        User.LEVEL_ALL = level_all_value

        original_level_label = getattr(User, "_original_level_label_for_all_policy", None)
        if original_level_label is None:
            original_level_label = User.level_label
            User._original_level_label_for_all_policy = original_level_label

        def level_label_with_all(cls, level_value: str) -> str:
            if level_value == level_all_value:
                return level_all_label
            try:
                return original_level_label(level_value)
            except TypeError:
                return original_level_label(cls, level_value)

        User.level_label = classmethod(level_label_with_all)

        original_can_book_level = getattr(User, "_original_can_book_level_for_all_policy", None)
        if original_can_book_level is None:
            original_can_book_level = User.can_book_level
            User._original_can_book_level_for_all_policy = original_can_book_level

        def can_book_level_with_all(self, target_level: str) -> bool:
            if target_level == level_all_value:
                return True
            return original_can_book_level(self, target_level)

        User.can_book_level = can_book_level_with_all

        original_can_book_any_level = getattr(User, "_original_can_book_any_level_for_all_policy", None)
        if original_can_book_any_level is None:
            original_can_book_any_level = User.can_book_any_level
            User._original_can_book_any_level_for_all_policy = original_can_book_any_level

        def can_book_any_level_with_all(self, *target_levels: str) -> bool:
            levels = [level for level in target_levels if level]
            if not levels:
                return True
            if level_all_value in levels:
                return True
            return original_can_book_any_level(self, *levels)

        User.can_book_any_level = can_book_any_level_with_all

        for model_class in (CoachAvailability, FixedLesson, Reservation):
            target_level_field = model_class._meta.get_field("target_level")
            target_level_field.choices = target_level_choices

            target_level_2_field = model_class._meta.get_field("target_level_2")
            target_level_2_field.choices = optional_target_level_choices

        _LEVEL_ALL_POLICY_PATCHED = True
    except Exception:
        return



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


def _patch_availability_save_policy():
    """
    管理画面や同期処理で一般レッスンのコーチ人数を変更した際、
    update_fields 指定があっても capacity / court_count まで確実に保存します。
    """
    global _AVAILABILITY_SAVE_POLICY_PATCHED

    if _AVAILABILITY_SAVE_POLICY_PATCHED:
        return

    try:
        from .models import CoachAvailability
    except Exception:
        return

    original_save = getattr(CoachAvailability, "_original_save_for_capacity_policy", None)
    if original_save is None:
        original_save = CoachAvailability.save
        CoachAvailability._original_save_for_capacity_policy = original_save

    def save_with_capacity_policy(self, *args, **kwargs):
        update_fields = kwargs.get("update_fields")
        if update_fields is not None and getattr(self, "lesson_type", "") == getattr(self, "LESSON_GENERAL", "general"):
            update_field_set = set(update_fields)
            update_field_set.update({"capacity", "coach_count", "court_count"})
            kwargs["update_fields"] = list(update_field_set)

        return original_save(self, *args, **kwargs)

    CoachAvailability.save = save_with_capacity_policy
    _AVAILABILITY_SAVE_POLICY_PATCHED = True


def _patch_fixed_lesson_sync_policy():
    """
    固定レッスンの管理画面保存時に、既存の参加者を消さずに
    担当コーチ・定員・対象レベル・コートを今後の予約へ反映します。

    改修ポイント:
    - 固定参加メンバーではない通常エントリー済み顧客をキャンセルしない。
    - 担当コーチ変更時、既存予約・キャンセル待ちも新担当コーチへ引き継ぐ。
    - コーチ人数を2→1に戻した場合、一般レッスン定員を12→6へ戻す。
    """
    global _FIXED_LESSON_SYNC_POLICY_PATCHED

    if _FIXED_LESSON_SYNC_POLICY_PATCHED:
        return

    try:
        from .models import CoachAvailability, FixedLesson, LessonWaitlist, Reservation, TicketLedger
    except Exception:
        return

    original_fixed_save = getattr(FixedLesson, "_original_save_for_fixed_sync_policy", None)
    if original_fixed_save is None:
        original_fixed_save = FixedLesson.save
        FixedLesson._original_save_for_fixed_sync_policy = original_fixed_save

    def fixed_lesson_save_with_clean(self, *args, **kwargs):
        # ModelAdmin以外の保存でも、コーチ人数・定員・必要コート数を必ず自動補正します。
        try:
            self.full_clean()
        except Exception:
            # 通常のDjango保存挙動に合わせるため、検証エラーは元のsave側へ渡さずそのまま再送出します。
            raise

        update_fields = kwargs.get("update_fields")
        if update_fields is not None:
            update_field_set = set(update_fields)
            update_field_set.update({"coach_count", "court_count", "capacity", "target_level_2"})
            kwargs["update_fields"] = list(update_field_set)

        return original_fixed_save(self, *args, **kwargs)

    FixedLesson.save = fixed_lesson_save_with_clean

    def _safe_primary_coach(fixed_lesson):
        try:
            return fixed_lesson.primary_coach()
        except Exception:
            return getattr(fixed_lesson, "coach", None)

    def _safe_build_datetimes(fixed_lesson, target_date):
        try:
            return fixed_lesson._build_datetimes_for_date(target_date)
        except Exception:
            start_hour = int(getattr(fixed_lesson, "start_hour", 0) or 0)
            start_dt = datetime.combine(target_date, datetime.min.time()).replace(hour=start_hour, minute=0)
            if timezone.is_naive(start_dt):
                start_dt = timezone.make_aware(start_dt)
            duration_hours = 2 if getattr(fixed_lesson, "lesson_type", "") == getattr(fixed_lesson, "LESSON_GENERAL", "general") else 1
            return start_dt, start_dt + timedelta(hours=duration_hours)

    def _scheduled_dates(fixed_lesson):
        try:
            return list(fixed_lesson.scheduled_occurrence_dates())
        except Exception:
            repeat_start = getattr(fixed_lesson, "start_date", None) or timezone.localdate()
            weekday = int(getattr(fixed_lesson, "weekday", repeat_start.weekday()) or repeat_start.weekday())
            offset = (weekday - repeat_start.weekday()) % 7
            count = max(int(getattr(fixed_lesson, "weeks_ahead", 1) or 1), 1)
            first_date = repeat_start + timedelta(days=offset)
            return [first_date + timedelta(days=7 * index) for index in range(count)]

    def _target_capacity(fixed_lesson, fixed_member_count):
        try:
            effective_capacity = int(fixed_lesson.effective_capacity())
        except Exception:
            effective_capacity = int(getattr(fixed_lesson, "capacity", 0) or 0)
        return max(effective_capacity, int(fixed_member_count or 0), 1)

    def _availability_for_fixed_slot(fixed_lesson, *, primary_coach, start_at, end_at, capacity):
        defaults = {
            "capacity": capacity,
            "coach_count": int(getattr(fixed_lesson, "coach_count", 1) or 1),
            "court_count": int(getattr(fixed_lesson, "court_count", 1) or 1),
            "target_level": getattr(fixed_lesson, "target_level", ""),
            "target_level_2": getattr(fixed_lesson, "target_level_2", "") or "",
            "status": CoachAvailability.STATUS_OPEN,
            "note": f"固定レッスン: {getattr(fixed_lesson, 'title', '') or fixed_lesson.get_weekday_display()}",
        }

        availability = (
            CoachAvailability.objects.filter(
                coach=primary_coach,
                court=fixed_lesson.court,
                lesson_type=fixed_lesson.lesson_type,
                start_at=start_at,
                end_at=end_at,
            )
            .order_by("id")
            .first()
        )

        if availability is None:
            availability = CoachAvailability(
                coach=primary_coach,
                court=fixed_lesson.court,
                lesson_type=fixed_lesson.lesson_type,
                start_at=start_at,
                end_at=end_at,
                **defaults,
            )
            availability.save()
            return availability, True

        updated_fields = []
        for field_name, value in defaults.items():
            if getattr(availability, field_name) != value:
                setattr(availability, field_name, value)
                updated_fields.append(field_name)

        if updated_fields:
            availability.save(update_fields=updated_fields)

        return availability, False

    def _update_reservation_slot(reservation, *, fixed_lesson, availability, primary_coach):
        update_fields = []

        desired_values = {
            "coach": primary_coach,
            "substitute_coach": availability.substitute_coach,
            "court": fixed_lesson.court,
            "availability": availability,
            "lesson_type": fixed_lesson.lesson_type,
            "target_level": fixed_lesson.target_level,
            "target_level_2": getattr(fixed_lesson, "target_level_2", "") or "",
            "custom_ticket_price": getattr(availability, "custom_ticket_price", 0),
            "custom_duration_hours": getattr(availability, "custom_duration_hours", 0),
        }

        for field_name, value in desired_values.items():
            current_id = getattr(reservation, f"{field_name}_id", None)
            value_id = getattr(value, "pk", None)
            if value_id is not None:
                if current_id != value_id:
                    setattr(reservation, field_name, value)
                    update_fields.append(field_name)
            else:
                if getattr(reservation, field_name) != value:
                    setattr(reservation, field_name, value)
                    update_fields.append(field_name)

        if update_fields:
            reservation.save(update_fields=update_fields)
            return 1
        return 0

    def _update_waitlist_slot(waitlist, *, fixed_lesson, availability, primary_coach):
        update_fields = []

        desired_values = {
            "coach": primary_coach,
            "substitute_coach": availability.substitute_coach,
            "court": fixed_lesson.court,
            "availability": availability,
            "lesson_type": fixed_lesson.lesson_type,
            "target_level": fixed_lesson.target_level,
            "target_level_2": getattr(fixed_lesson, "target_level_2", "") or "",
        }

        for field_name, value in desired_values.items():
            current_id = getattr(waitlist, f"{field_name}_id", None)
            value_id = getattr(value, "pk", None)
            if value_id is not None:
                if current_id != value_id:
                    setattr(waitlist, field_name, value)
                    update_fields.append(field_name)
            else:
                if getattr(waitlist, field_name) != value:
                    setattr(waitlist, field_name, value)
                    update_fields.append(field_name)

        if update_fields:
            waitlist.save(update_fields=update_fields)
            return 1
        return 0

    def sync_future_reservations_safe(self, created_by=None):
        if not getattr(self, "is_active", False):
            return 0
        if not getattr(self, "court_id", None):
            return 0

        changed_count = 0
        today = timezone.localdate()
        primary_coach = _safe_primary_coach(self)
        if not primary_coach:
            return 0

        target_dates = _scheduled_dates(self)
        target_datetimes = {
            _safe_build_datetimes(self, target_date)
            for target_date in target_dates
        }

        members = list(self.members.all())
        member_ids = {member.pk for member in members}
        required_capacity = _target_capacity(self, len(members))

        # 開催回数変更などで対象外になった「固定参加の自動予約」だけを整理します。
        # 通常エントリー済み顧客は is_fixed_entry=False のため、ここでは消しません。
        extra_fixed_reservations = Reservation.objects.filter(
            fixed_lesson=self,
            is_fixed_entry=True,
            start_at__date__gte=today,
            status=Reservation.STATUS_ACTIVE,
        )
        for reservation in extra_fixed_reservations:
            if (reservation.start_at, reservation.end_at) in target_datetimes:
                continue
            try:
                reservation.cancel(
                    created_by=created_by,
                    reason="固定レッスンの開催回数変更による自動整理",
                )
                changed_count += 1
            except Exception:
                continue

        for target_date in target_dates:
            if target_date < today:
                continue

            start_at, end_at = _safe_build_datetimes(self, target_date)
            availability, created = _availability_for_fixed_slot(
                self,
                primary_coach=primary_coach,
                start_at=start_at,
                end_at=end_at,
                capacity=required_capacity,
            )
            if created:
                changed_count += 1

            # 担当コーチ変更時に、既存の通常エントリー・固定参加予約を新しい枠へ引き継ぎます。
            existing_reservations = Reservation.objects.filter(
                fixed_lesson=self,
                start_at=start_at,
                end_at=end_at,
                status__in=[Reservation.STATUS_ACTIVE, Reservation.STATUS_PENDING],
            ).select_related("user", "coach", "substitute_coach", "court", "availability")
            for reservation in existing_reservations:
                changed_count += _update_reservation_slot(
                    reservation,
                    fixed_lesson=self,
                    availability=availability,
                    primary_coach=primary_coach,
                )

            existing_waitlists = LessonWaitlist.objects.filter(
                fixed_lesson=self,
                start_at=start_at,
                end_at=end_at,
                status=LessonWaitlist.STATUS_WAITING,
            ).select_related("user", "coach", "substitute_coach", "court", "availability")
            for waitlist in existing_waitlists:
                changed_count += _update_waitlist_slot(
                    waitlist,
                    fixed_lesson=self,
                    availability=availability,
                    primary_coach=primary_coach,
                )

            # 固定参加メンバーから外された会員の「固定参加の自動予約」だけをキャンセルします。
            obsolete_fixed_reservations = Reservation.objects.filter(
                fixed_lesson=self,
                is_fixed_entry=True,
                start_at=start_at,
                end_at=end_at,
                status=Reservation.STATUS_ACTIVE,
            ).exclude(user_id__in=member_ids)
            for reservation in obsolete_fixed_reservations:
                try:
                    reservation.cancel(created_by=created_by, reason="固定レッスンメンバー解除")
                    changed_count += 1
                except Exception:
                    continue

            for member in members:
                existing = (
                    Reservation.objects.filter(
                        user=member,
                        fixed_lesson=self,
                        start_at=start_at,
                        end_at=end_at,
                        status=Reservation.STATUS_ACTIVE,
                    )
                    .order_by("id")
                    .first()
                )

                if existing:
                    if not getattr(existing, "is_fixed_entry", False):
                        existing.is_fixed_entry = True
                        try:
                            existing.save(update_fields=["is_fixed_entry"])
                            changed_count += 1
                        except Exception:
                            pass
                    changed_count += _update_reservation_slot(
                        existing,
                        fixed_lesson=self,
                        availability=availability,
                        primary_coach=primary_coach,
                    )
                    continue

                reservation = Reservation(
                    user=member,
                    coach=primary_coach,
                    substitute_coach=availability.substitute_coach,
                    court=self.court,
                    availability=availability,
                    fixed_lesson=self,
                    is_fixed_entry=True,
                    lesson_type=self.lesson_type,
                    target_level=self.target_level,
                    target_level_2=getattr(self, "target_level_2", "") or "",
                    start_at=start_at,
                    end_at=end_at,
                    status=Reservation.STATUS_ACTIVE,
                    custom_ticket_price=getattr(availability, "custom_ticket_price", 0),
                    custom_duration_hours=getattr(availability, "custom_duration_hours", 0),
                )

                try:
                    with transaction.atomic():
                        reservation.full_clean()
                        reservation.save()
                        if int(getattr(reservation, "tickets_used", 0) or 0) > 0:
                            reservation.consume_tickets(
                                reason=TicketLedger.REASON_FIXED_USE,
                                created_by=created_by,
                                note=f"固定レッスン自動登録: {getattr(self, 'title', '') or self.get_weekday_display()}",
                            )
                        changed_count += 1
                except Exception:
                    continue

            # 旧担当コーチ側に残った、固定レッスン由来の空き枠は重複表示防止のため削除します。
            old_availability_qs = CoachAvailability.objects.filter(
                court=self.court,
                lesson_type=self.lesson_type,
                start_at=start_at,
                end_at=end_at,
                note__startswith="固定レッスン:",
            ).exclude(pk=availability.pk)
            for old_availability in old_availability_qs:
                has_live_reservations = Reservation.objects.filter(
                    availability=old_availability,
                    status__in=[Reservation.STATUS_ACTIVE, Reservation.STATUS_PENDING],
                ).exists()
                has_live_waitlists = LessonWaitlist.objects.filter(
                    availability=old_availability,
                    status=LessonWaitlist.STATUS_WAITING,
                ).exists()
                if has_live_reservations or has_live_waitlists:
                    continue
                try:
                    old_availability.delete()
                    changed_count += 1
                except Exception:
                    continue

        return changed_count

    FixedLesson.sync_future_reservations = sync_future_reservations_safe
    _FIXED_LESSON_SYNC_POLICY_PATCHED = True


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



def _repair_fixed_lesson_slots_for_request(request):
    """
    固定レッスンの現在設定を、実データ側のレッスン枠・予約・キャンセル待ちへ同期します。

    目的:
    - FixedLesson が正、CoachAvailability / Reservation / LessonWaitlist が従。
    - コーチ人数2→1なら、一般レッスン定員12→6へ実データも戻す。
    - 担当コーチ変更時、既存参加者を消さず新担当コーチへ引き継ぐ。
    - 古い CoachAvailability が残っても、固定レッスン由来の枠は現在設定へ寄せる。
    """
    path = getattr(request, "path", "") or ""
    should_repair = (
        path.startswith("/lesson-calendar/")
        or path.startswith("/admin/club/fixedlesson/")
        or path.startswith("/admin/club/coachavailability/")
    )
    if not should_repair:
        return

    try:
        from .models import CoachAvailability, FixedLesson, LessonWaitlist, Reservation
    except Exception:
        return

    today = timezone.localdate()

    try:
        target_year = _parse_int(request.GET.get("year") or request.POST.get("year"), today.year)
        target_month = _parse_int(request.GET.get("month") or request.POST.get("month"), today.month)
        if target_month < 1 or target_month > 12:
            target_month = today.month
        range_start, range_end = _month_start_end(target_year, target_month)
    except Exception:
        range_start = today
        range_end = today + timedelta(days=120)

    # 管理画面保存直後の確認にも効くよう、少し先まで同期対象を広げます。
    if path.startswith("/admin/club/fixedlesson/"):
        range_start = min(range_start, today)
        range_end = max(range_end, today + timedelta(days=120))

    try:
        fixed_lessons = (
            FixedLesson.objects.filter(is_active=True)
            .select_related("coach", "coach_2", "coach_3", "court")
            .prefetch_related("members")
            .order_by("weekday", "start_hour", "id")
        )
    except Exception:
        return

    def _safe_primary_coach(fixed_lesson):
        try:
            return fixed_lesson.primary_coach()
        except Exception:
            return getattr(fixed_lesson, "coach", None)

    def _safe_capacity(fixed_lesson):
        try:
            effective_capacity = int(fixed_lesson.effective_capacity())
        except Exception:
            effective_capacity = int(getattr(fixed_lesson, "capacity", 0) or 0)

        try:
            member_count = fixed_lesson.members.count()
        except Exception:
            member_count = 0

        return max(effective_capacity, int(member_count or 0), 1)

    def _safe_int(value, default=1):
        try:
            return int(value or default)
        except Exception:
            return default

    for fixed_lesson in fixed_lessons:
        if not getattr(fixed_lesson, "court_id", None):
            continue

        primary_coach = _safe_primary_coach(fixed_lesson)
        if not primary_coach:
            continue

        try:
            occurrence_dates = _fixed_occurrence_dates(fixed_lesson, range_start, range_end)
        except Exception:
            occurrence_dates = []

        for target_date in occurrence_dates:
            start_at, end_at = _fixed_lesson_datetimes_safely(fixed_lesson, target_date)
            if not start_at or not end_at:
                continue

            capacity = _safe_capacity(fixed_lesson)
            coach_count = max(_safe_int(getattr(fixed_lesson, "coach_count", 1), 1), 1)
            court_count = max(_safe_int(getattr(fixed_lesson, "court_count", coach_count), coach_count), 1)
            target_level = getattr(fixed_lesson, "target_level", "") or ""
            target_level_2 = getattr(fixed_lesson, "target_level_2", "") or ""
            lesson_type = getattr(fixed_lesson, "lesson_type", "") or ""
            note_text = f"固定レッスン: {getattr(fixed_lesson, 'title', '') or fixed_lesson.get_weekday_display()}"

            slot_availability_qs = CoachAvailability.objects.filter(
                court=fixed_lesson.court,
                lesson_type=lesson_type,
                start_at=start_at,
                end_at=end_at,
            ).order_by("id")

            availability = slot_availability_qs.filter(coach=primary_coach).first()
            if availability is None:
                availability = slot_availability_qs.filter(note__startswith="固定レッスン:").first()
            if availability is None:
                availability = slot_availability_qs.first()

            if availability is None:
                try:
                    availability = CoachAvailability.objects.create(
                        coach=primary_coach,
                        court=fixed_lesson.court,
                        lesson_type=lesson_type,
                        target_level=target_level,
                        target_level_2=target_level_2,
                        start_at=start_at,
                        end_at=end_at,
                        capacity=capacity,
                        coach_count=coach_count,
                        court_count=court_count,
                        status=CoachAvailability.STATUS_OPEN,
                        note=note_text,
                    )
                except Exception:
                    continue
            else:
                update_values = {}
                if getattr(availability, "coach_id", None) != getattr(primary_coach, "pk", None):
                    update_values["coach"] = primary_coach
                if getattr(availability, "court_id", None) != getattr(fixed_lesson.court, "pk", None):
                    update_values["court"] = fixed_lesson.court
                if getattr(availability, "capacity", None) != capacity:
                    update_values["capacity"] = capacity
                if getattr(availability, "coach_count", None) != coach_count:
                    update_values["coach_count"] = coach_count
                if getattr(availability, "court_count", None) != court_count:
                    update_values["court_count"] = court_count
                if getattr(availability, "target_level", "") != target_level:
                    update_values["target_level"] = target_level
                if getattr(availability, "target_level_2", "") != target_level_2:
                    update_values["target_level_2"] = target_level_2
                if getattr(availability, "lesson_type", "") != lesson_type:
                    update_values["lesson_type"] = lesson_type
                if not getattr(availability, "note", ""):
                    update_values["note"] = note_text

                if update_values:
                    try:
                        CoachAvailability.objects.filter(pk=availability.pk).update(**update_values)
                        for field_name, value in update_values.items():
                            setattr(availability, field_name, value)
                    except Exception:
                        continue

            # 予約済み・承認待ちの顧客はキャンセルせず、現在の固定レッスン枠へ付け替えます。
            try:
                Reservation.objects.filter(
                    fixed_lesson=fixed_lesson,
                    start_at=start_at,
                    end_at=end_at,
                    status__in=[Reservation.STATUS_ACTIVE, Reservation.STATUS_PENDING],
                ).update(
                    coach=primary_coach,
                    substitute_coach=getattr(availability, "substitute_coach", None),
                    court=fixed_lesson.court,
                    availability=availability,
                    lesson_type=lesson_type,
                    target_level=target_level,
                    target_level_2=target_level_2,
                    custom_ticket_price=getattr(availability, "custom_ticket_price", 0),
                    custom_duration_hours=getattr(availability, "custom_duration_hours", 0),
                )
            except Exception:
                pass

            try:
                LessonWaitlist.objects.filter(
                    fixed_lesson=fixed_lesson,
                    start_at=start_at,
                    end_at=end_at,
                    status=LessonWaitlist.STATUS_WAITING,
                ).update(
                    coach=primary_coach,
                    substitute_coach=getattr(availability, "substitute_coach", None),
                    court=fixed_lesson.court,
                    availability=availability,
                    lesson_type=lesson_type,
                    target_level=target_level,
                    target_level_2=target_level_2,
                )
            except Exception:
                pass

            # 固定レッスン由来の古い空き枠は、参加者を引き継いだ後なら削除します。
            try:
                old_availability_qs = slot_availability_qs.filter(note__startswith="固定レッスン:").exclude(pk=availability.pk)
                for old_availability in old_availability_qs:
                    has_reservations = Reservation.objects.filter(
                        availability=old_availability,
                        status__in=[Reservation.STATUS_ACTIVE, Reservation.STATUS_PENDING],
                    ).exists()
                    has_waitlists = LessonWaitlist.objects.filter(
                        availability=old_availability,
                        status=LessonWaitlist.STATUS_WAITING,
                    ).exists()
                    if has_reservations or has_waitlists:
                        continue
                    old_availability.delete()
            except Exception:
                pass


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




def _inject_family_profile_nav_button(request, html):
    """
    家族受講者プロフィール管理画面への導線を追加します。

    base.htmlの巨大置換を避けるため、HTML応答時に以下を補正します。
    - PC/タブレット用の上部メニューに「家族プロフィール」を追加
    - スマホ用の下部ナビに「家族」を追加
    """
    user = getattr(request, "user", None)
    if not user or not getattr(user, "is_authenticated", False):
        return html

    if 'href="/family/"' in html:
        return html

    role = getattr(user, "role", "")
    can_use_family_profile = (
        role in ("member", "contractor_coach")
        or bool(getattr(user, "is_staff", False))
        or bool(getattr(user, "is_superuser", False))
    )
    if not can_use_family_profile:
        return html

    is_family_page = request.path.startswith("/family/")
    family_active_class = " nav-primary" if is_family_page else ""
    family_top_link = f'<a href="/family/" class="{family_active_class.strip()}">家族プロフィール</a>'
    family_bottom_active_class = " is-active" if is_family_page else ""
    family_bottom_link = (
        f'<a href="/family/" class="{family_bottom_active_class.strip()}">'
        '<span class="bottom-icon">👪</span><span>家族</span>'
        '</a>'
    )

    # スマホ下部ナビは、家族ボタン追加に合わせて5列から6列へ広げます。
    html = html.replace(
        "grid-template-columns:repeat(5, minmax(0, 1fr));",
        "grid-template-columns:repeat(6, minmax(0, 1fr));",
        1,
    )

    # 会員メニュー: 「チケット」の前に追加すると予約導線の近くで見つけやすい。
    member_ticket_marker = '              <a href="/tickets/">'
    if member_ticket_marker in html:
        html = html.replace(member_ticket_marker, f"              {family_top_link}\n{member_ticket_marker}", 1)

    # コーチ/業務委託コーチ用の上部メニュー: 「使い方」の前に追加。
    top_help_marker = '              <a href="/help/">使い方</a>'
    if top_help_marker in html:
        html = html.replace(top_help_marker, f"              {family_top_link}\n{top_help_marker}", 1)

    # 会員スマホ下部ナビ: 「チケット」の前に追加。
    member_bottom_ticket_marker = '        <a href="/tickets/"'
    if member_bottom_ticket_marker in html:
        html = html.replace(member_bottom_ticket_marker, f"        {family_bottom_link}\n{member_bottom_ticket_marker}", 1)

    # コーチ/業務委託コーチスマホ下部ナビ: 「予約承認」の前に追加。
    coach_bottom_reservation_marker = '        <a href="/reservations/"'
    if coach_bottom_reservation_marker in html:
        html = html.replace(coach_bottom_reservation_marker, f"        {family_bottom_link}\n{coach_bottom_reservation_marker}", 1)

    return html


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

    def process_request(self, request):
        _patch_court_type_choices()
        _patch_all_level_choices()
        _patch_preopen_last_cancel_policy()
        _patch_availability_save_policy()
        _patch_fixed_lesson_sync_policy()
        _repair_fixed_lesson_slots_for_request(request)
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

        html = _inject_lesson_calendar_notice_courts_and_holidays(request, html)
        html = _inject_family_profile_nav_button(request, html)

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
