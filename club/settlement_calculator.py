def reservation_coaches_for_split(reservation):
    substitute = getattr(reservation, "substitute_coach", None)
    if substitute and getattr(substitute, "role", "") in (
        "coach",
        "contractor_coach",
    ):
        return [substitute]

    fixed_lesson = getattr(reservation, "fixed_lesson", None)
    if fixed_lesson:
        try:
            coaches = [
                coach
                for coach in fixed_lesson.all_coaches()
                if coach
                and getattr(coach, "role", "")
                in ("coach", "contractor_coach")
            ]
            if coaches:
                return coaches
        except Exception:
            pass

    assigned = None
    try:
        assigned = reservation.assigned_coach()
    except Exception:
        assigned = (
            getattr(reservation, "substitute_coach", None)
            or getattr(reservation, "coach", None)
        )

    if assigned and getattr(assigned, "role", "") in (
        "coach",
        "contractor_coach",
    ):
        return [assigned]
    return []


def reservation_duration_minutes(reservation):
    try:
        return max(
            int(
                (reservation.end_at - reservation.start_at).total_seconds()
                // 60
            ),
            0,
        )
    except Exception:
        return 0


def reservation_slot_key(reservation, coach):
    return (
        str(reservation.lesson_type or ""),
        str(getattr(reservation, "court_id", "") or ""),
        reservation.start_at.isoformat() if reservation.start_at else "",
        reservation.end_at.isoformat() if reservation.end_at else "",
        str(getattr(coach, "pk", "") or ""),
    )


def stringing_is_cancelled(order):
    raw = str(getattr(order, "status", "") or "").lower()
    return "cancel" in raw or "キャンセル" in raw
