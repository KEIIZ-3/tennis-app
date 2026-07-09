import re
from datetime import timedelta

from django.db import connection
from django.utils import timezone

from .family_reservations import resolve_reservation_participant


class WaitlistParticipantMiddleware:
    """
    キャンセル待ち登録時に選択された参加者を、LessonWaitlistとは別テーブルに保存します。

    views.py は巨大なため直接変更せず、既存のキャンセル待ち作成処理が完了した後に、
    request.POST の participant_key と直近の LessonWaitlist を紐づけます。
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)

        try:
            if request.method == "POST" and getattr(request, "user", None) and request.user.is_authenticated:
                action = (request.POST.get("action") or "").strip()
                if action == "join_waitlist":
                    self._save_join_waitlist_participant(request)

                if self._is_promote_waitlist_path(request.path):
                    self._copy_waitlist_participant_to_reservation(request)
        except Exception:
            pass

        return response

    def _save_join_waitlist_participant(self, request):
        participant_key = (request.POST.get("participant_key") or "self").strip()
        participant = resolve_reservation_participant(request.user, participant_key)

        waitlist = self._find_recent_waitlist_for_request(request)
        if not waitlist:
            return

        self._upsert_waitlist_participant_snapshot(waitlist, participant)

    def _copy_waitlist_participant_to_reservation(self, request):
        waitlist_id = self._waitlist_id_from_path(request.path)
        if not waitlist_id:
            return

        waitlist_row = self._waitlist_row(waitlist_id)
        if not waitlist_row:
            return

        participant = self._waitlist_participant_snapshot(waitlist_id)
        if not participant:
            return

        reservation_id = self._find_promoted_reservation_id(waitlist_row)
        if not reservation_id:
            return

        self._upsert_reservation_participant_snapshot(
            reservation_id=reservation_id,
            parent_id=waitlist_row["user_id"],
            participant=participant,
        )

    def _find_recent_waitlist_for_request(self, request):
        from .models import LessonWaitlist

        qs = LessonWaitlist.objects.filter(
            user=request.user,
            status=LessonWaitlist.STATUS_WAITING,
        ).order_by("-created_at", "-id")

        availability_id = (request.POST.get("availability_id") or "").strip()
        fixed_lesson_id = (request.POST.get("fixed_lesson_id") or "").strip()
        lesson_date = (request.POST.get("lesson_date") or "").strip()

        if availability_id:
            qs = qs.filter(availability_id=availability_id)

        if fixed_lesson_id:
            qs = qs.filter(fixed_lesson_id=fixed_lesson_id)

        if lesson_date:
            qs = qs.filter(start_at__date=lesson_date)

        threshold = timezone.now() - timedelta(minutes=5)
        recent = qs.filter(created_at__gte=threshold).first()
        return recent or qs.first()

    def _upsert_waitlist_participant_snapshot(self, waitlist, participant):
        now = timezone.now()

        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO club_lessonwaitlistparticipant
                    (
                        waitlist_id,
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
                ON CONFLICT (waitlist_id)
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
                    waitlist.pk,
                    waitlist.user_id,
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

    def _is_promote_waitlist_path(self, path):
        return bool(re.search(r"/waitlists/\d+/promote/?$", str(path or "")))

    def _waitlist_id_from_path(self, path):
        match = re.search(r"/waitlists/(\d+)/promote/?$", str(path or ""))
        if not match:
            return None
        try:
            return int(match.group(1))
        except Exception:
            return None

    def _waitlist_row(self, waitlist_id):
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    id,
                    user_id,
                    coach_id,
                    court_id,
                    lesson_type,
                    start_at,
                    end_at
                FROM club_lessonwaitlist
                WHERE id = %s
                LIMIT 1
                """,
                [waitlist_id],
            )
            row = cursor.fetchone()

        if not row:
            return None

        return {
            "id": row[0],
            "user_id": row[1],
            "coach_id": row[2],
            "court_id": row[3],
            "lesson_type": row[4],
            "start_at": row[5],
            "end_at": row[6],
        }

    def _waitlist_participant_snapshot(self, waitlist_id):
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    family_member_id,
                    participant_type,
                    participant_name,
                    participant_level,
                    participant_level_label,
                    relationship_label
                FROM club_lessonwaitlistparticipant
                WHERE waitlist_id = %s
                LIMIT 1
                """,
                [waitlist_id],
            )
            row = cursor.fetchone()

        if not row:
            return None

        return {
            "family_member_id": row[0],
            "type": row[1] or "self",
            "name": row[2] or "",
            "level": row[3] or "",
            "level_label": row[4] or "",
            "relationship_label": row[5] or "",
        }

    def _find_promoted_reservation_id(self, waitlist_row):
        threshold = timezone.now() - timedelta(minutes=10)

        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id
                FROM club_reservation
                WHERE user_id = %s
                  AND coach_id = %s
                  AND court_id = %s
                  AND lesson_type = %s
                  AND start_at = %s
                  AND end_at = %s
                  AND status = 'active'
                  AND created_at >= %s
                ORDER BY created_at DESC, id DESC
                LIMIT 1
                """,
                [
                    waitlist_row["user_id"],
                    waitlist_row["coach_id"],
                    waitlist_row["court_id"],
                    waitlist_row["lesson_type"],
                    waitlist_row["start_at"],
                    waitlist_row["end_at"],
                    threshold,
                ],
            )
            row = cursor.fetchone()

        if row:
            return row[0]

        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id
                FROM club_reservation
                WHERE user_id = %s
                  AND coach_id = %s
                  AND court_id = %s
                  AND lesson_type = %s
                  AND start_at = %s
                  AND end_at = %s
                  AND status = 'active'
                ORDER BY created_at DESC, id DESC
                LIMIT 1
                """,
                [
                    waitlist_row["user_id"],
                    waitlist_row["coach_id"],
                    waitlist_row["court_id"],
                    waitlist_row["lesson_type"],
                    waitlist_row["start_at"],
                    waitlist_row["end_at"],
                ],
            )
            row = cursor.fetchone()

        return row[0] if row else None

    def _upsert_reservation_participant_snapshot(self, reservation_id, parent_id, participant):
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
                    reservation_id,
                    parent_id,
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
