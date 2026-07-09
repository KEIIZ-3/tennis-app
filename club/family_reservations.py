from django.core.exceptions import ValidationError
from django.db import connection
from django.utils import timezone


PARTICIPANT_SELF = "self"
PARTICIPANT_FAMILY_PREFIX = "family:"


def _level_choices(user_model):
    return tuple(getattr(user_model, "LEVEL_CHOICES", ())) or (
        ("family", "ファミリー"),
        ("beginner", "初級"),
        ("beginner_plus", "初中級"),
        ("intermediate", "中級"),
        ("intermediate_plus", "中上級"),
        ("advanced", "上級"),
    )


def _level_label(user_model, level_value):
    try:
        return user_model.level_label(level_value)
    except Exception:
        return dict(_level_choices(user_model)).get(level_value, level_value or "")


def _level_rank(user_model, level_value):
    try:
        return int(getattr(user_model, "LEVEL_ORDER", {}).get(level_value, 0) or 0)
    except Exception:
        return 0


def _can_book_level(user_model, participant_level, target_level):
    if not target_level or target_level == "all":
        return True
    participant_rank = _level_rank(user_model, participant_level)
    target_rank = _level_rank(user_model, target_level)
    if target_rank <= 0:
        return True
    return participant_rank >= target_rank


def participant_can_book(user_model, participant_level, *target_levels):
    levels = [level for level in target_levels if level]
    if not levels:
        return True
    if "all" in levels:
        return True
    return any(_can_book_level(user_model, participant_level, level) for level in levels)


def _parent_display_name(user):
    try:
        return user.display_name()
    except Exception:
        return getattr(user, "username", "") or "本人"


def _active_family_rows(parent):
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT id, full_name, relationship, member_level
            FROM club_familymember
            WHERE parent_id = %s
              AND is_active = TRUE
            ORDER BY full_name ASC, id ASC
            """,
            [parent.pk],
        )
        return cursor.fetchall()


def build_participant_choices_for_user(parent, target_level="", target_level_2=""):
    user_model = parent.__class__
    parent_level = getattr(parent, "member_level", "") or ""
    parent_can_book = participant_can_book(user_model, parent_level, target_level, target_level_2)

    choices = [
        {
            "key": PARTICIPANT_SELF,
            "type": "self",
            "family_member_id": "",
            "name": _parent_display_name(parent),
            "relationship_label": "本人",
            "level": parent_level,
            "level_label": _level_label(user_model, parent_level),
            "can_book": parent_can_book,
            "disabled_reason": "" if parent_can_book else "このレッスンの対象レベル外です。",
        }
    ]

    for member_id, full_name, relationship, member_level in _active_family_rows(parent):
        can_book = participant_can_book(user_model, member_level, target_level, target_level_2)
        choices.append(
            {
                "key": f"{PARTICIPANT_FAMILY_PREFIX}{member_id}",
                "type": "family",
                "family_member_id": member_id,
                "name": full_name,
                "relationship_label": _relationship_label(relationship),
                "level": member_level,
                "level_label": _level_label(user_model, member_level),
                "can_book": can_book,
                "disabled_reason": "" if can_book else "このレッスンの対象レベル外です。",
            }
        )

    return choices


def _relationship_label(value):
    return {
        "child": "子供",
        "spouse": "配偶者",
        "parent": "親",
        "other": "その他",
    }.get(value, value or "家族")


def resolve_reservation_participant(parent, participant_key):
    participant_key = (participant_key or PARTICIPANT_SELF).strip()
    user_model = parent.__class__

    if participant_key == PARTICIPANT_SELF:
        level = getattr(parent, "member_level", "") or ""
        return {
            "key": PARTICIPANT_SELF,
            "type": "self",
            "family_member_id": None,
            "name": _parent_display_name(parent),
            "relationship": "self",
            "relationship_label": "本人",
            "level": level,
            "level_label": _level_label(user_model, level),
        }

    if not participant_key.startswith(PARTICIPANT_FAMILY_PREFIX):
        raise ValidationError("参加者の選択が不正です。")

    raw_id = participant_key.replace(PARTICIPANT_FAMILY_PREFIX, "", 1)
    try:
        family_member_id = int(raw_id)
    except Exception:
        raise ValidationError("参加者の選択が不正です。")

    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT id, full_name, relationship, member_level
            FROM club_familymember
            WHERE id = %s
              AND parent_id = %s
              AND is_active = TRUE
            """,
            [family_member_id, parent.pk],
        )
        row = cursor.fetchone()

    if not row:
        raise ValidationError("選択された受講者プロフィールが見つかりません。")

    member_id, full_name, relationship, member_level = row
    return {
        "key": f"{PARTICIPANT_FAMILY_PREFIX}{member_id}",
        "type": "family",
        "family_member_id": member_id,
        "name": full_name,
        "relationship": relationship,
        "relationship_label": _relationship_label(relationship),
        "level": member_level,
        "level_label": _level_label(user_model, member_level),
    }


def validate_participant_can_book_lesson(participant, target_level="", target_level_2=""):
    # family member の parent は予約レコードの user で管理するため、ここではレベルのみ検証する。
    # level_label は保存済みの表示用で、比較には level を使う。
    class DummyUserModel:
        LEVEL_ORDER = {
            "family": 1,
            "beginner": 2,
            "beginner_plus": 3,
            "intermediate": 4,
            "intermediate_plus": 5,
            "advanced": 6,
        }

    participant_level = participant.get("level", "") or ""
    if participant_can_book(DummyUserModel, participant_level, target_level, target_level_2):
        return

    name = participant.get("name") or "選択された参加者"
    raise ValidationError(f"{name}さんのレベルでは、このレッスンは予約できません。")


def save_reservation_participant_snapshot(reservation, participant):
    now = timezone.now()

    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO club_reservationparticipant
                (
                    reservation_id,
                    parent_id,
                    family_member_id,
                    participant_type,
                    participant_name,
                    participant_level,
                    participant_level_label,
                    relationship_label,
                    created_at,
                    updated_at
                )
            VALUES
                (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (reservation_id)
            DO UPDATE SET
                parent_id = EXCLUDED.parent_id,
                family_member_id = EXCLUDED.family_member_id,
                participant_type = EXCLUDED.participant_type,
                participant_name = EXCLUDED.participant_name,
                participant_level = EXCLUDED.participant_level,
                participant_level_label = EXCLUDED.participant_level_label,
                relationship_label = EXCLUDED.relationship_label,
                updated_at = EXCLUDED.updated_at
            """,
            [
                reservation.pk,
                reservation.user_id,
                participant.get("family_member_id"),
                participant.get("type") or "self",
                participant.get("name") or "",
                participant.get("level") or "",
                participant.get("level_label") or "",
                participant.get("relationship_label") or "",
                now,
                now,
            ],
        )
