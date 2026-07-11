from django import template
from django.db import connection

register = template.Library()


def _safe_display_name(user):
    if not user:
        return "-"
    try:
        return user.display_name()
    except Exception:
        return getattr(user, "full_name", "") or getattr(user, "username", "") or str(user)


def _safe_phone(user):
    if not user:
        return ""
    return (
        getattr(user, "phone_number", "")
        or getattr(user, "phone", "")
        or getattr(user, "tel", "")
        or ""
    )


def _fallback_participant_from_parent(parent):
    parent_name = _safe_display_name(parent)
    return {
        "name": parent_name,
        "level_label": "",
        "relationship_label": "本人",
        "parent_name": parent_name,
        "parent_phone": _safe_phone(parent),
        "is_family": False,
        "has_snapshot": False,
    }


def _fallback_participant(reservation):
    parent = getattr(reservation, "user", None)
    return _fallback_participant_from_parent(parent)


def _empty_participant():
    return {
        "name": "-",
        "level_label": "",
        "relationship_label": "",
        "parent_name": "-",
        "parent_phone": "",
        "is_family": False,
        "has_snapshot": False,
    }


def _normalize_participant_row(row, fallback):
    if not row:
        return fallback

    participant_name, participant_level_label, relationship_label, participant_type = row
    participant_name = participant_name or fallback["name"]
    participant_level_label = participant_level_label or fallback["level_label"]
    relationship_label = relationship_label or fallback["relationship_label"]
    participant_type = participant_type or "self"

    is_family = participant_type == "family" or relationship_label not in ("", "本人", "self")

    return {
        "name": participant_name,
        "level_label": participant_level_label,
        "relationship_label": relationship_label,
        "parent_name": fallback["parent_name"],
        "parent_phone": fallback["parent_phone"],
        "is_family": is_family,
        "has_snapshot": True,
    }


@register.simple_tag
def participant_for_reservation(reservation):
    if not reservation:
        return _empty_participant()

    fallback = _fallback_participant(reservation)
    reservation_id = getattr(reservation, "pk", None)
    if not reservation_id:
        return fallback

    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    participant_name,
                    participant_level_label,
                    relationship_label,
                    participant_type
                FROM club_reservationparticipant
                WHERE reservation_id = %s
                LIMIT 1
                """,
                [reservation_id],
            )
            row = cursor.fetchone()
    except Exception:
        return fallback

    if not row:
        return fallback

    return _normalize_participant_row(row, fallback)


@register.simple_tag
def participant_for_waitlist(waitlist):
    if not waitlist:
        return _empty_participant()

    parent = getattr(waitlist, "user", None)
    fallback = _fallback_participant_from_parent(parent)
    waitlist_id = getattr(waitlist, "pk", None)
    if not waitlist_id:
        return fallback

    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    participant_name,
                    participant_level_label,
                    relationship_label,
                    participant_type
                FROM club_lessonwaitlistparticipant
                WHERE waitlist_id = %s
                LIMIT 1
                """,
                [waitlist_id],
            )
            row = cursor.fetchone()
    except Exception:
        return fallback

    if not row:
        return fallback

    return _normalize_participant_row(row, fallback)
