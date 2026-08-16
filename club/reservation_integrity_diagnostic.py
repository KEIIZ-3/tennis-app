"""Read-only, occurrence-level Reservation source-of-truth diagnostics."""

from collections import defaultdict

from django.utils import timezone

from . import lesson_execution
from .lesson_execution_storage import read_status_map
from .models import FixedLesson, Reservation
from .settlement_models import MonthlySettlement


SEVERITY_OK = "OK"
SEVERITY_WARNING = "WARNING"
SEVERITY_ERROR = "ERROR"
SEVERITY_HISTORICAL = "HISTORICAL"
SEVERITY_UNKNOWN = "UNKNOWN"


def occurrence_key(reservation):
    local_date = timezone.localtime(reservation.start_at).date()
    if reservation.fixed_lesson_id:
        return f"fixed:{reservation.fixed_lesson_id}:{local_date.isoformat()}"
    if reservation.availability_id:
        return f"availability:{reservation.availability_id}"
    return "legacy:{lesson_type}:{coach}:{court}:{start}:{end}".format(
        lesson_type=reservation.lesson_type,
        coach=reservation.coach_id,
        court=reservation.court_id,
        start=reservation.start_at.isoformat(),
        end=reservation.end_at.isoformat(),
    )


def _capacity(rows):
    sample = rows[0]
    source = sample.fixed_lesson or sample.availability
    if source is None:
        return None
    return int(source.effective_capacity())


def _finding(category, severity, key, rows, **details):
    return {
        "category": category,
        "severity": severity,
        "occurrence": key,
        "reservation_ids": [row.pk for row in rows],
        "user_ids": [row.user_id for row in rows],
        **details,
    }


def diagnose_reservation_integrity(*, today=None):
    """Return serializable findings. This function performs SELECTs only."""
    today = today or timezone.localdate()
    reservations = list(
        Reservation.objects.select_related("fixed_lesson", "availability")
        .order_by("start_at", "id")
    )
    grouped = defaultdict(list)
    for reservation in reservations:
        grouped[occurrence_key(reservation)].append(reservation)

    status_maps = {}
    for settlement in MonthlySettlement.objects.only(
        "year", "month", "note", "calculation_snapshot"
    ).order_by("year", "month", "id"):
        status_maps.update(read_status_map(settlement))

    findings = []
    occurrence_rows = []
    for key, rows in sorted(grouped.items()):
        active = [row for row in rows if row.status == Reservation.STATUS_ACTIVE]
        pending = [row for row in rows if row.status == Reservation.STATUS_PENDING]
        canceled = [
            row for row in rows
            if row.status in (Reservation.STATUS_CANCELED, Reservation.STATUS_RAIN_CANCELED)
        ]
        capacity = _capacity(rows)
        cancellation_type = lesson_execution._cancellation_evidence(rows)
        cancellation_intent = next((
            lesson_execution.CANCELLATION_TYPE_RAIN
            if (
                row.status == Reservation.STATUS_RAIN_CANCELED
                or "雨天中止" in str(row.cancellation_reason or "")
            )
            else lesson_execution.CANCELLATION_TYPE_OTHER
            for row in canceled
            if (
                row.status == Reservation.STATUS_RAIN_CANCELED
                or "雨天中止" in str(row.cancellation_reason or "")
                or "レッスン中止" in str(row.cancellation_reason or "")
            )
        ), None)
        execution_entry = status_maps.get(key, {})
        execution_status = execution_entry.get("status")
        occurrence_rows.append({
            "occurrence": key,
            "active_count": len(active),
            "pending_count": len(pending),
            "canceled_count": len(canceled),
            "capacity": capacity,
            "execution_status": execution_status,
            "cancellation_type": cancellation_type,
        })
        if capacity is not None and len(active) > capacity:
            findings.append(_finding(
                "J_CAPACITY_EXCEEDED", SEVERITY_ERROR, key, active,
                active_count=len(active), capacity=capacity,
            ))
        if cancellation_intent and active:
            findings.append(_finding(
                "M_LESSON_CANCELED_WITH_ACTIVE_RESERVATION", SEVERITY_ERROR,
                key, rows, active_reservation_ids=[row.pk for row in active],
                cancellation_type=cancellation_intent,
            ))
        if execution_status == lesson_execution.STATUS_HELD and cancellation_intent:
            findings.append(_finding(
                "N_HELD_CANCELLATION_CONFLICT", SEVERITY_ERROR, key, rows,
                cancellation_type=cancellation_intent,
            ))
        saved_cancellation_type = execution_entry.get("cancellation_type")
        if cancellation_type and saved_cancellation_type and cancellation_type != saved_cancellation_type:
            findings.append(_finding(
                "Q_R_CANCELLATION_LABEL_MISMATCH", SEVERITY_ERROR, key, rows,
                reservation_cancellation_type=cancellation_type,
                execution_cancellation_type=saved_cancellation_type,
            ))

    future_fixed_lessons = FixedLesson.objects.filter(is_active=True).prefetch_related("members")
    reservation_pairs = {
        (row.fixed_lesson_id, row.user_id, timezone.localtime(row.start_at).date())
        for row in reservations
        if row.fixed_lesson_id and row.status in (Reservation.STATUS_ACTIVE, Reservation.STATUS_PENDING)
    }
    for fixed_lesson in future_fixed_lessons:
        members = list(fixed_lesson.members.all())
        for target_date in fixed_lesson.scheduled_occurrence_dates():
            if target_date < today:
                continue
            missing = [
                member for member in members
                if (fixed_lesson.pk, member.pk, target_date) not in reservation_pairs
            ]
            if missing:
                key = f"fixed:{fixed_lesson.pk}:{target_date.isoformat()}"
                findings.append({
                    "category": "F_FIXED_MEMBER_WITHOUT_FUTURE_RESERVATION",
                    "severity": SEVERITY_ERROR,
                    "occurrence": key,
                    "fixed_lesson_id": fixed_lesson.pk,
                    "reservation_ids": [],
                    "user_ids": [member.pk for member in missing],
                })

    severity_counts = {name: 0 for name in (
        SEVERITY_OK, SEVERITY_WARNING, SEVERITY_ERROR,
        SEVERITY_HISTORICAL, SEVERITY_UNKNOWN,
    )}
    for finding in findings:
        severity_counts[finding["severity"]] += 1
    if not findings:
        severity_counts[SEVERITY_OK] = len(occurrence_rows)

    return {
        "generated_at": timezone.now().isoformat(),
        "read_only": True,
        "repair_performed": False,
        "reservation_statuses": [value for value, _label in Reservation.STATUS_CHOICES],
        "occurrence_count": len(occurrence_rows),
        "occurrences": occurrence_rows,
        "finding_count": len(findings),
        "severity_counts": severity_counts,
        "findings": findings,
    }
