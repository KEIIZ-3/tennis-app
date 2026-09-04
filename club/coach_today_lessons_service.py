from collections import defaultdict
from datetime import datetime, timedelta
from urllib.parse import urlencode

from django.db.models import Q
from django.urls import reverse
from django.utils import timezone

from .lesson_participants import participant_details_by_reservation
from .models import CoachAvailability, FixedLesson, LessonWaitlist, Reservation
from .participant_levels import current_participant_level_label


def build_today_lessons_display(
    *,
    user,
    selected_coach,
    range_start,
    range_end,
    today,
    execution_pending_only,
    full_path,
    _display_name,
    _slot_key,
    _coach_can_manage_waitlist,
    _fixed_lesson_includes_coach,
    _lesson_level_label,
    _fixed_lesson_coach_names,
    _lesson_calendar_title,
):
    slot_map = {}
    reservations_by_availability = defaultdict(list)
    reservations_by_fixed_lesson = defaultdict(list)
    reservations_by_physical_slot = defaultdict(list)
    waitlists_by_availability = defaultdict(list)
    waitlists_by_fixed_lesson = defaultdict(list)
    waitlists_by_physical_slot = defaultdict(list)
    participant_details = {}

    def _slot_key_for_row(
        *, lesson_type, coach_id, court_id, start_at, end_at,
        fixed_lesson_id=None, availability_id=None,
    ):
        physical_key = _slot_key(lesson_type, coach_id, court_id, start_at, end_at)
        if fixed_lesson_id:
            return (*physical_key, "fixed_lesson", fixed_lesson_id)
        if availability_id:
            return (*physical_key, "availability", availability_id)
        return (*physical_key, "slot")

    def _local(value):
        if timezone.is_aware(value):
            return timezone.localtime(value)
        return value

    def _safe_phone(user):
        return (getattr(user, "phone_number", "") or "").strip()

    def _safe_level(user):
        try:
            return user.get_member_level_display()
        except Exception:
            return getattr(user, "member_level", "") or "-"

    def _reservation_person_row(reservation, participant_details=None):
        participant_details = participant_details or {}
        is_family_participant = participant_details.get("participant_type") == "family"
        payment_status_options = [
            (Reservation.PAYMENT_STATUS_UNPAID, "未回収"),
            (Reservation.PAYMENT_STATUS_PAID, "回収済み"),
            (Reservation.PAYMENT_STATUS_WAIVED, "免除"),
        ]
        return {
            "reservation": reservation,
            "name": participant_details.get("participant_name") or _display_name(reservation.user),
            "guardian_name": _display_name(reservation.user),
            "relationship_label": participant_details.get("relationship_label") or "本人",
            "is_family_participant": is_family_participant,
            "phone": _safe_phone(reservation.user),
            "level": current_participant_level_label(
                reservation.user,
                participant_type=participant_details.get("participant_type", "self"),
                family_member_id=participant_details.get("family_member_id"),
                snapshot_level_label=participant_details.get("participant_level_label", ""),
                is_guest=bool(reservation.guest_name),
            ),
            "status_label": reservation.get_status_display(),
            "detail_url": reverse("club:reservation_detail", kwargs={"pk": reservation.pk}),
            "payment_required": reservation.is_payment_tracking_required(),
            "payment_status": reservation.payment_status,
            "payment_status_label": reservation.payment_status_badge_label(),
            "payment_amount": int(reservation.payment_amount or 0),
            "payment_received_at": reservation.payment_received_at,
            "payment_status_options": payment_status_options,
        }

    def _waitlist_person_row(waitlist):
        return {
            "waitlist": waitlist,
            "name": _display_name(waitlist.user),
            "phone": _safe_phone(waitlist.user),
            "level": _safe_level(waitlist.user),
            "created_at": waitlist.created_at,
            "can_promote": _coach_can_manage_waitlist(user, waitlist),
        }

    def _add_slot(
        *,
        key,
        start_at,
        end_at,
        lesson_type_label,
        target_level_label,
        coach_name,
        court_name,
        capacity,
        title,
        fixed_lesson=None,
        availability=None,
    ):
        if key in slot_map:
            return slot_map[key]

        physical_key = (key[0], key[1] or None, key[2] or None, start_at, end_at)
        if availability is not None:
            slot_reservations = list(reservations_by_availability[availability.pk])
            if fixed_lesson is not None:
                competing_ids = _competing_fixed_lesson_ids(fixed_lesson, start_at, end_at)
                if competing_ids:
                    slot_reservations = [
                        reservation for reservation in slot_reservations
                        if reservation.fixed_lesson_id not in competing_ids
                    ]
        elif fixed_lesson is not None:
            slot_reservations = list(reservations_by_fixed_lesson[fixed_lesson.pk])
        else:
            slot_reservations = list(reservations_by_physical_slot[physical_key])
        reservations = [
            reservation for reservation in slot_reservations
            if reservation.status == Reservation.STATUS_ACTIVE
        ]
        pending_reservations = [
            reservation for reservation in slot_reservations
            if reservation.status == Reservation.STATUS_PENDING
        ]

        if fixed_lesson is not None:
            waitlists = list(waitlists_by_fixed_lesson[fixed_lesson.pk])
        elif availability is not None:
            waitlists = list(waitlists_by_availability[availability.pk])
        else:
            waitlists = list(waitlists_by_physical_slot[physical_key])
        participant_rows = [
            _reservation_person_row(reservation, participant_details.get(reservation.pk))
            for reservation in reservations
        ]

        # 固定レッスンの担当変更後は、予約作成時の旧コーチではなく、
        # レッスンカレンダーと同じ固定レッスン側の担当コーチを優先します。
        if fixed_lesson is not None:
            try:
                fixed_coach_name = fixed_lesson.coach_display_names()
            except Exception:
                fixed_coach_name = ""

            if fixed_coach_name and fixed_coach_name != "-":
                coach_name = fixed_coach_name

        elif availability is not None:
            try:
                availability_coach = availability.assigned_coach()
            except Exception:
                availability_coach = (
                    getattr(availability, "substitute_coach", None)
                    or getattr(availability, "coach", None)
                )

            availability_coach_name = _display_name(availability_coach)
            if availability_coach_name and availability_coach_name != "-":
                coach_name = availability_coach_name

        else:
            actual_coach_names = []
            for reservation in reservations:
                try:
                    actual_coach = reservation.assigned_coach()
                except Exception:
                    actual_coach = (
                        getattr(reservation, "substitute_coach", None)
                        or getattr(reservation, "coach", None)
                    )

                actual_coach_name = _display_name(actual_coach)
                if (
                    actual_coach_name
                    and actual_coach_name != "-"
                    and actual_coach_name not in actual_coach_names
                ):
                    actual_coach_names.append(actual_coach_name)

            if actual_coach_names:
                coach_name = " / ".join(actual_coach_names)

        start_local = _local(start_at)
        end_local = _local(end_at)
        lesson_date = start_local.date()
        participant_count = len(participant_rows)
        remaining_count = max(int(capacity or 0) - participant_count, 0)
        is_today = lesson_date == today
        is_past = end_at < timezone.now()
        is_full = participant_count >= int(capacity or 0)
        has_waitlist = bool(waitlists)
        needs_attention = bool(pending_reservations or waitlists or (is_today and remaining_count > 0))

        row = {
            "key": "|".join([str(part) for part in key]),
            "start_at": start_at,
            "end_at": end_at,
            "date": lesson_date,
            "date_label": f"{lesson_date:%Y/%m/%d}",
            "weekday_label": ["月", "火", "水", "木", "金", "土", "日"][lesson_date.weekday()],
            "time_label": f"{start_local:%H:%M}〜{end_local:%H:%M}",
            "title": title,
            "lesson_type_label": lesson_type_label,
            "target_level_label": target_level_label,
            "coach_name": coach_name,
            "court_name": court_name,
            "capacity": int(capacity or 0),
            "reservations": reservations,
            "pending_reservations": pending_reservations,
            "waitlists": waitlists,
            "participant_rows": participant_rows,
            "pending_rows": [_reservation_person_row(reservation, participant_details.get(reservation.pk)) for reservation in pending_reservations],
            "waitlist_rows": [_waitlist_person_row(waitlist) for waitlist in waitlists],
            "registered_member_rows": [],
            "participant_count": participant_count,
            "pending_count": len(pending_reservations),
            "waitlist_count": len(waitlists),
            "remaining_count": remaining_count,
            "is_today": is_today,
            "is_past": is_past,
            "is_full": is_full,
            "has_waitlist": has_waitlist,
            "needs_attention": needs_attention,
            "status_label": "本日" if is_today else ("終了" if is_past else "予定"),
            "fixed_lesson": fixed_lesson,
            "availability": availability,
        }
        slot_map[key] = row
        return row

    def _availability_capacity(availability):
        try:
            return max(int(availability.effective_capacity()), int(availability.capacity or 0), 1)
        except Exception:
            return max(int(getattr(availability, "capacity", 1) or 1), 1)

    def _authoritative_fixed_lesson_for_availability(availability):
        """
        過去に担当変更されたAvailabilityでも、同一枠の予約が保持するFixedLessonを正本として返します。

        FixedLessonの開催回数、曜日設定、表示期間、コーチ絞り込みの影響で
        fixed_queryset側から先に行が作られなかった場合でも、旧Availability単独行として
        旧コーチ名を表示させないための最終防御です。
        """
        if not availability:
            return None

        linked_reservation = next(
            (
                reservation for reservation in reservations_by_availability[availability.pk]
                if reservation.fixed_lesson_id
                and reservation.start_at == availability.start_at
                and reservation.end_at == availability.end_at
            ),
            None,
        )
        if linked_reservation is None:
            linked_reservation = next(
                (
                    reservation for reservation in all_period_reservations
                    if reservation.fixed_lesson_id
                    and reservation.lesson_type == availability.lesson_type
                    and reservation.start_at == availability.start_at
                    and reservation.end_at == availability.end_at
                    and (
                        reservation.court_id == availability.court_id
                        or reservation.availability_id == availability.pk
                    )
                ),
                None,
            )

        if linked_reservation is not None:
            return linked_reservation.fixed_lesson

        start_local = _local(availability.start_at)
        target_date = start_local.date()

        candidates = [
            fixed_lesson for fixed_lesson in all_fixed_lessons
            if fixed_lesson.is_active
            and fixed_lesson.lesson_type == availability.lesson_type
            and fixed_lesson.start_hour == start_local.hour
            and (
                not availability.court_id
                or fixed_lesson.court_id in (availability.court_id, None)
            )
        ]

        for fixed_lesson in candidates:
            try:
                if target_date in _scheduled_dates(fixed_lesson):
                    return fixed_lesson
            except Exception:
                repeat_start = getattr(fixed_lesson, "start_date", None)
                if repeat_start and target_date < repeat_start:
                    continue
                if int(getattr(fixed_lesson, "weekday", -1)) == target_date.weekday():
                    return fixed_lesson

        return None

    all_fixed_lessons = list(
        FixedLesson.objects.filter(is_active=True)
        .select_related("coach", "coach_2", "coach_3", "court")
        .prefetch_related("members", "canceled_occurrences")
        .order_by("weekday", "start_hour", "id")
    )
    fixed_queryset = all_fixed_lessons
    if selected_coach is not None:
        fixed_queryset = [fixed for fixed in fixed_queryset if _fixed_lesson_includes_coach(fixed, selected_coach)]

    all_period_availabilities = list(
        CoachAvailability.objects.filter(
            start_at__date__gte=range_start,
            start_at__date__lte=range_end,
        )
        .select_related("coach", "substitute_coach", "court")
        .order_by("start_at", "id")
    )
    availability_qs = all_period_availabilities
    if selected_coach is not None:
        availability_qs = [
            availability for availability in availability_qs
            if availability.coach_id == selected_coach.pk
            or availability.substitute_coach_id == selected_coach.pk
        ]

    period_start = timezone.make_aware(datetime.combine(range_start, datetime.min.time()))
    period_end = timezone.make_aware(datetime.combine(range_end + timedelta(days=1), datetime.min.time()))
    reservation_queryset = Reservation.objects.filter(
        start_at__gte=period_start,
        start_at__lt=period_end,
    )
    waitlist_queryset = LessonWaitlist.objects.filter(
        status=LessonWaitlist.STATUS_WAITING,
        start_at__gte=period_start,
        start_at__lt=period_end,
    )
    if selected_coach is not None:
        relevant_fixed_ids = [fixed.pk for fixed in fixed_queryset]
        relevant_availability_ids = [availability.pk for availability in availability_qs]
        coach_scope = (
            Q(coach=selected_coach)
            | Q(substitute_coach=selected_coach)
            | Q(fixed_lesson_id__in=relevant_fixed_ids)
            | Q(availability_id__in=relevant_availability_ids)
        )
        reservation_queryset = reservation_queryset.filter(coach_scope)
        waitlist_queryset = waitlist_queryset.filter(coach_scope)

    all_period_reservations = list(
        reservation_queryset
        .select_related(
            "user", "coach", "substitute_coach", "court", "availability",
            "fixed_lesson", "fixed_lesson__coach", "fixed_lesson__coach_2",
            "fixed_lesson__coach_3", "fixed_lesson__court",
        )
        .order_by("guest_name", "user__full_name", "user__username", "id")
    )
    display_reservations = [
        reservation for reservation in all_period_reservations
        if reservation.status in (Reservation.STATUS_ACTIVE, Reservation.STATUS_PENDING)
    ]
    for reservation in display_reservations:
        if reservation.availability_id:
            reservations_by_availability[reservation.availability_id].append(reservation)
        if reservation.fixed_lesson_id:
            reservations_by_fixed_lesson[reservation.fixed_lesson_id].append(reservation)
        reservations_by_physical_slot[(
            reservation.lesson_type, reservation.coach_id, reservation.court_id,
            reservation.start_at, reservation.end_at,
        )].append(reservation)
    participant_details = participant_details_by_reservation(display_reservations)

    all_waitlists = list(
        waitlist_queryset
        .select_related("user", "coach", "substitute_coach", "court", "availability", "fixed_lesson")
        .order_by("created_at", "id")
    )
    for waitlist in all_waitlists:
        if waitlist.availability_id:
            waitlists_by_availability[waitlist.availability_id].append(waitlist)
        if waitlist.fixed_lesson_id:
            waitlists_by_fixed_lesson[waitlist.fixed_lesson_id].append(waitlist)
        waitlists_by_physical_slot[(
            waitlist.lesson_type, waitlist.coach_id, waitlist.court_id,
            waitlist.start_at, waitlist.end_at,
        )].append(waitlist)

    for reservation in all_period_reservations:
        if reservation.fixed_lesson and all(
            fixed.pk != reservation.fixed_lesson_id for fixed in all_fixed_lessons
        ):
            all_fixed_lessons.append(reservation.fixed_lesson)

    competing_fixed_lesson_ids = {}
    scheduled_dates_by_fixed_lesson = {}

    def _scheduled_dates(fixed_lesson):
        if fixed_lesson.pk not in scheduled_dates_by_fixed_lesson:
            scheduled_dates_by_fixed_lesson[fixed_lesson.pk] = set(
                fixed_lesson.scheduled_occurrence_dates()
            )
        return scheduled_dates_by_fixed_lesson[fixed_lesson.pk]

    def _competing_fixed_lesson_ids(fixed_lesson, start_at, end_at):
        cache_key = (fixed_lesson.pk, start_at, end_at)
        if cache_key not in competing_fixed_lesson_ids:
            target_date = _local(start_at).date()
            current_title = (fixed_lesson.title or "").strip()
            ids = set()
            for candidate in all_fixed_lessons:
                if not candidate.is_active or candidate.pk == fixed_lesson.pk:
                    continue
                if current_title and (candidate.title or "").strip() == current_title:
                    continue
                if target_date not in _scheduled_dates(candidate):
                    continue
                candidate_start, candidate_end = candidate._build_datetimes_for_date(target_date)
                if candidate_start == start_at and candidate_end == end_at:
                    ids.add(candidate.pk)
            competing_fixed_lesson_ids[cache_key] = ids
        return competing_fixed_lesson_ids[cache_key]

    availability_by_physical_slot = defaultdict(list)
    for availability in all_period_availabilities:
        availability_by_physical_slot[(
            availability.court_id, availability.lesson_type,
            availability.start_at, availability.end_at,
        )].append(availability)

    # レッスンカレンダーと同じ開催日生成ロジックを使用します。
    # weekdayだけで判定すると、個別に設定された開催日や既存データの曜日差異により、
    # FixedLessonが認識されず旧Availability単独行として表示されるためです。
    for fixed in fixed_queryset:
        try:
            if hasattr(fixed, "scheduled_occurrence_dates"):
                occurrence_dates = list(_scheduled_dates(fixed))
            else:
                repeat_start = getattr(fixed, "start_date", None) or range_start
                first_offset = (int(fixed.weekday) - repeat_start.weekday()) % 7
                first_date = repeat_start + timedelta(days=first_offset)
                occurrence_count = max(
                    int(getattr(fixed, "weeks_ahead", 1) or 1),
                    1,
                )
                occurrence_dates = [
                    first_date + timedelta(days=7 * index)
                    for index in range(occurrence_count)
                ]
        except Exception:
            occurrence_dates = []

        for cursor in occurrence_dates:
            if cursor < range_start or cursor > range_end:
                continue

            try:
                start_at, end_at = fixed._build_datetimes_for_date(cursor)
            except Exception:
                continue

            primary_coach = fixed.primary_coach() if hasattr(fixed, "primary_coach") else fixed.coach
            court = fixed.court
            if not court:
                continue

            # 固定レッスンの担当変更前に作成された旧Availabilityも、
            # 参加者・回収状況を同じ枠へ統合するために取得します。
            # 現在の担当コーチとの一致は条件にせず、日時・種別・コートを正とします。
            matching_availabilities = availability_by_physical_slot[(
                court.pk, fixed.lesson_type, start_at, end_at,
            )]
            availability = matching_availabilities[0] if matching_availabilities else None

            capacity = fixed.effective_capacity() if hasattr(fixed, "effective_capacity") else fixed.capacity
            if availability:
                try:
                    capacity = max(
                        int(availability.effective_capacity()),
                        int(availability.capacity or 0),
                        int(capacity or 0),
                        1,
                    )
                except Exception:
                    capacity = max(int(availability.capacity or 0), int(capacity or 0), 1)

            # 旧Availabilityがある場合は、そのコーチIDをスロットキーに使います。
            # これにより後段のAvailability一覧で同じ枠が重複追加されません。
            # 表示する担当名は _add_slot 内で現在のFixedLesson設定へ置き換えます。
            slot_coach_id = (
                availability.coach_id
                if availability is not None
                else getattr(primary_coach, "pk", None)
            )

            key = _slot_key_for_row(
                lesson_type=fixed.lesson_type,
                coach_id=slot_coach_id,
                court_id=getattr(court, "pk", None),
                start_at=start_at,
                end_at=end_at,
                fixed_lesson_id=fixed.pk,
            )

            _add_slot(
                key=key,
                start_at=start_at,
                end_at=end_at,
                lesson_type_label=fixed.get_lesson_type_display(),
                target_level_label=_lesson_level_label(fixed) or fixed.get_target_level_display(),
                coach_name=_fixed_lesson_coach_names(fixed),
                court_name=str(court),
                capacity=capacity,
                title=_lesson_calendar_title(fixed),
                fixed_lesson=fixed,
                availability=availability,
            )

    for availability in availability_qs:
        authoritative_fixed_lesson = _authoritative_fixed_lesson_for_availability(
            availability
        )

        # 旧データでは固定レッスンだけが削除され、生成枠が残る場合がある。
        # カレンダーから消えた開催を当日精算だけに復活させない。
        if (
            authoritative_fixed_lesson is None
            and (availability.note or "").startswith("固定レッスン:")
        ):
            continue

        # 過去の担当変更後も、予約が紐づく現在のFixedLessonを表示の正本にします。
        # 選択中のコーチ条件も、旧Availabilityのコーチではなく現在の固定レッスン担当で判定します。
        if authoritative_fixed_lesson is not None:
            if (
                selected_coach is not None
                and not _fixed_lesson_includes_coach(
                    authoritative_fixed_lesson,
                    selected_coach,
                )
            ):
                continue

            key = _slot_key_for_row(
                lesson_type=availability.lesson_type,
                coach_id=availability.coach_id,
                court_id=availability.court_id,
                start_at=availability.start_at,
                end_at=availability.end_at,
                fixed_lesson_id=authoritative_fixed_lesson.pk,
            )
            if key in slot_map:
                continue

            fixed_capacity = (
                authoritative_fixed_lesson.effective_capacity()
                if hasattr(authoritative_fixed_lesson, "effective_capacity")
                else authoritative_fixed_lesson.capacity
            )
            capacity = max(
                _availability_capacity(availability),
                int(fixed_capacity or 0),
                1,
            )

            _add_slot(
                key=key,
                start_at=availability.start_at,
                end_at=availability.end_at,
                lesson_type_label=authoritative_fixed_lesson.get_lesson_type_display(),
                target_level_label=(
                    _lesson_level_label(authoritative_fixed_lesson)
                    or authoritative_fixed_lesson.get_target_level_display()
                ),
                coach_name=_fixed_lesson_coach_names(
                    authoritative_fixed_lesson
                ),
                court_name=str(
                    authoritative_fixed_lesson.court
                    or availability.court
                ),
                capacity=capacity,
                title=_lesson_calendar_title(
                    authoritative_fixed_lesson
                ),
                fixed_lesson=authoritative_fixed_lesson,
                availability=availability,
            )
            continue

        key = _slot_key_for_row(
            lesson_type=availability.lesson_type,
            coach_id=availability.coach_id,
            court_id=availability.court_id,
            start_at=availability.start_at,
            end_at=availability.end_at,
            availability_id=availability.pk,
        )
        if key in slot_map:
            continue

        capacity = _availability_capacity(availability)
        assigned_coach = (
            availability.assigned_coach()
            if hasattr(availability, "assigned_coach")
            else (availability.substitute_coach or availability.coach)
        )

        _add_slot(
            key=key,
            start_at=availability.start_at,
            end_at=availability.end_at,
            lesson_type_label=availability.get_lesson_type_display(),
            target_level_label=_lesson_level_label(availability) or availability.get_target_level_display(),
            coach_name=availability.coach_display_names(),
            court_name=str(availability.court),
            capacity=capacity,
            title=availability.get_lesson_type_display(),
            availability=availability,
        )

    lesson_rows = sorted(slot_map.values(), key=lambda row: (row["start_at"], row["title"], row["key"]))
    from . import lesson_execution

    year_month_pairs = {
        (row["date"].year, row["date"].month)
        for row in lesson_rows
        if row.get("availability")
    }
    execution_status_map = lesson_execution.status_by_availability(
        user,
        year_month_pairs,
    )
    from .court_expense_transfer import (
        court_transfer_summary_for_availability,
    )

    for row in lesson_rows:
        availability = row.get("availability")
        status = (
            execution_status_map.get(availability.pk)
            if availability is not None
            else None
        )
        if status:
            row.update(status)
            row["needs_attention"] = bool(
                row["needs_attention"]
                or status["execution_needs_attention"]
            )
        if availability is not None:
            court_summary = court_transfer_summary_for_availability(
                availability
            )
            row.update(
                {
                    "court_status": court_summary["status"],
                    "court_status_label": court_summary["status_label"],
                    "court_amount": court_summary["amount"],
                    "court_payer_name": court_summary["payer_name"],
                    "court_expense_url": (
                        f"{reverse('club:coach_expense_manage')}?"
                        f"{urlencode({
                            'availability_id': availability.pk,
                            'date': row['date'].isoformat(),
                            'next': full_path,
                        })}"
                    ),
                }
            )

    if execution_pending_only:
        lesson_rows = [
            row for row in lesson_rows
            if row.get("execution_needs_attention")
        ]
    today_rows = [row for row in lesson_rows if row["date"] == today]
    upcoming_rows = [row for row in lesson_rows if row["date"] != today]
    attention_rows = [row for row in lesson_rows if row["needs_attention"] and not row["is_past"]]

    # 過去1か月表示では、終了済みレッスンも含めて参加費の回収状況を編集できるようにします。
    for row in lesson_rows:
        payment_rows = [
            person for person in row["participant_rows"]
            if person.get("payment_required")
        ]
        row["payment_target_count"] = len(payment_rows)
        row["payment_unpaid_count"] = sum(
            1 for person in payment_rows
            if person.get("payment_status") == Reservation.PAYMENT_STATUS_UNPAID
        )

    grouped_days = []
    day_cursor = range_start
    while day_cursor <= range_end:
        day_rows = [row for row in lesson_rows if row["date"] == day_cursor]
        grouped_days.append(
            {
                "date": day_cursor,
                "date_label": f"{day_cursor:%Y/%m/%d}",
                "weekday_label": ["月", "火", "水", "木", "金", "土", "日"][day_cursor.weekday()],
                "is_today": day_cursor == today,
                "rows": day_rows,
            }
        )
        day_cursor += timedelta(days=1)

    all_active_reservations = []
    for row in lesson_rows:
        all_active_reservations.extend(row["reservations"])

    payment_target_reservations = [
        reservation for reservation in all_active_reservations
        if reservation.is_payment_tracking_required()
    ]
    payment_paid_total = sum(
        int(reservation.payment_amount or 0)
        for reservation in payment_target_reservations
        if reservation.payment_status == Reservation.PAYMENT_STATUS_PAID
    )
    payment_unpaid_total = sum(
        int(reservation.payment_amount or 0)
        for reservation in payment_target_reservations
        if reservation.payment_status == Reservation.PAYMENT_STATUS_UNPAID
    )
    payment_waived_total = sum(
        int(reservation.payment_amount or 0)
        for reservation in payment_target_reservations
        if reservation.payment_status == Reservation.PAYMENT_STATUS_WAIVED
    )

    summary = {
        "lesson_count": len(lesson_rows),
        "today_lesson_count": len(today_rows),
        "participant_count": sum(row["participant_count"] for row in lesson_rows),
        "today_participant_count": sum(row["participant_count"] for row in today_rows),
        "waitlist_count": sum(row["waitlist_count"] for row in lesson_rows),
        "pending_count": sum(row["pending_count"] for row in lesson_rows),
        "attention_count": len(attention_rows),
        "payment_target_count": len(payment_target_reservations),
        "payment_paid_total": payment_paid_total,
        "payment_unpaid_total": payment_unpaid_total,
        "payment_waived_total": payment_waived_total,
    }

    return {
        "execution_pending_only": execution_pending_only,
        "grouped_days": grouped_days,
        "lesson_rows": lesson_rows,
        "today_rows": today_rows,
        "upcoming_rows": upcoming_rows,
        "attention_rows": attention_rows[:10],
        "summary": summary,
    }
