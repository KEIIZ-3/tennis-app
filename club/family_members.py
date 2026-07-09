from datetime import date

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth import get_user_model
from django.db import connection
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods


RELATIONSHIP_CHOICES = (
    ("child", "子供"),
    ("spouse", "配偶者"),
    ("parent", "親"),
    ("other", "その他"),
)


def _level_choices():
    User = get_user_model()
    return tuple(getattr(User, "LEVEL_CHOICES", ())) or (
        ("family", "ファミリー"),
        ("beginner", "初級"),
        ("elementary", "初中級"),
        ("intermediate", "中級"),
        ("upper_intermediate", "中上級"),
        ("advanced", "上級"),
    )


def _choice_label(choices, value):
    return dict(choices).get(value, value or "-")


def _table_name():
    return "club_familymember"


def _user_display_name(user):
    if not user:
        return "-"
    try:
        return user.display_name()
    except Exception:
        return getattr(user, "username", "-") or "-"


def _date_or_none(value):
    value = (value or "").strip()
    if not value:
        return None
    return date.fromisoformat(value)


def _family_member_rows(parent):
    with connection.cursor() as cursor:
        cursor.execute(
            f"""
            SELECT id, full_name, kana, relationship, birth_date, member_level, note, is_active, created_at, updated_at
            FROM {_table_name()}
            WHERE parent_id = %s
            ORDER BY is_active DESC, full_name ASC, id ASC
            """,
            [parent.pk],
        )
        rows = cursor.fetchall()

    level_choices = _level_choices()
    relationship_choices = RELATIONSHIP_CHOICES

    result = []
    for row in rows:
        (
            member_id,
            full_name,
            kana,
            relationship,
            birth_date,
            member_level,
            note,
            is_active,
            created_at,
            updated_at,
        ) = row
        result.append(
            {
                "id": member_id,
                "full_name": full_name,
                "kana": kana,
                "relationship": relationship,
                "relationship_label": _choice_label(relationship_choices, relationship),
                "birth_date": birth_date,
                "birth_date_value": birth_date.isoformat() if birth_date else "",
                "member_level": member_level,
                "member_level_label": _choice_label(level_choices, member_level),
                "note": note,
                "is_active": bool(is_active),
                "created_at": created_at,
                "updated_at": updated_at,
            }
        )
    return result


def _get_family_member(parent, member_id):
    try:
        member_id_int = int(member_id)
    except Exception:
        return None

    with connection.cursor() as cursor:
        cursor.execute(
            f"""
            SELECT id, full_name, kana, relationship, birth_date, member_level, note, is_active
            FROM {_table_name()}
            WHERE id = %s AND parent_id = %s
            """,
            [member_id_int, parent.pk],
        )
        row = cursor.fetchone()

    if not row:
        return None

    return {
        "id": row[0],
        "full_name": row[1],
        "kana": row[2],
        "relationship": row[3],
        "birth_date": row[4],
        "member_level": row[5],
        "note": row[6],
        "is_active": bool(row[7]),
    }


def _validate_payload(request):
    level_values = {value for value, _label in _level_choices()}
    relationship_values = {value for value, _label in RELATIONSHIP_CHOICES}

    full_name = (request.POST.get("full_name") or "").strip()
    kana = (request.POST.get("kana") or "").strip()
    relationship = (request.POST.get("relationship") or "child").strip()
    birth_date_raw = (request.POST.get("birth_date") or "").strip()
    member_level = (request.POST.get("member_level") or "").strip()
    note = (request.POST.get("note") or "").strip()

    if not full_name:
        raise ValueError("受講者名を入力してください。")

    if relationship not in relationship_values:
        raise ValueError("続柄が不正です。")

    if member_level not in level_values:
        raise ValueError("レベルを選択してください。")

    try:
        birth_date = _date_or_none(birth_date_raw)
    except Exception:
        raise ValueError("生年月日の形式が正しくありません。")

    if birth_date and birth_date > timezone.localdate():
        raise ValueError("生年月日は今日以前の日付で入力してください。")

    return {
        "full_name": full_name[:120],
        "kana": kana[:120],
        "relationship": relationship,
        "birth_date": birth_date,
        "member_level": member_level,
        "note": note[:1000],
    }


@login_required
@require_http_methods(["GET", "POST"])
def family_member_manage(request):
    if getattr(request.user, "role", "") not in ("member", "contractor_coach") and not (
        getattr(request.user, "is_staff", False) or getattr(request.user, "is_superuser", False)
    ):
        messages.error(request, "受講者プロフィール管理は会員アカウントで利用してください。")
        return redirect("club:home")

    if request.method == "POST":
        action = (request.POST.get("action") or "").strip()

        if action in ("create", "update"):
            try:
                payload = _validate_payload(request)
            except ValueError as exc:
                messages.error(request, str(exc))
                return redirect("club:family_member_manage")

            if action == "create":
                now = timezone.now()
                with connection.cursor() as cursor:
                    cursor.execute(
                        f"""
                        INSERT INTO {_table_name()}
                            (parent_id, full_name, kana, relationship, birth_date, member_level, note, is_active, created_at, updated_at)
                        VALUES
                            (%s, %s, %s, %s, %s, %s, %s, TRUE, %s, %s)
                        """,
                        [
                            request.user.pk,
                            payload["full_name"],
                            payload["kana"],
                            payload["relationship"],
                            payload["birth_date"],
                            payload["member_level"],
                            payload["note"],
                            now,
                            now,
                        ],
                    )
                messages.success(request, "受講者プロフィールを追加しました。")
                return redirect("club:family_member_manage")

            member = _get_family_member(request.user, request.POST.get("member_id"))
            if not member:
                messages.error(request, "対象の受講者プロフィールが見つかりません。")
                return redirect("club:family_member_manage")

            with connection.cursor() as cursor:
                cursor.execute(
                    f"""
                    UPDATE {_table_name()}
                    SET full_name = %s,
                        kana = %s,
                        relationship = %s,
                        birth_date = %s,
                        member_level = %s,
                        note = %s,
                        updated_at = %s
                    WHERE id = %s AND parent_id = %s
                    """,
                    [
                        payload["full_name"],
                        payload["kana"],
                        payload["relationship"],
                        payload["birth_date"],
                        payload["member_level"],
                        payload["note"],
                        timezone.now(),
                        member["id"],
                        request.user.pk,
                    ],
                )
            messages.success(request, "受講者プロフィールを更新しました。")
            return redirect("club:family_member_manage")

        if action == "toggle_active":
            member = _get_family_member(request.user, request.POST.get("member_id"))
            if not member:
                messages.error(request, "対象の受講者プロフィールが見つかりません。")
                return redirect("club:family_member_manage")

            next_active = not bool(member["is_active"])
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""
                    UPDATE {_table_name()}
                    SET is_active = %s,
                        updated_at = %s
                    WHERE id = %s AND parent_id = %s
                    """,
                    [next_active, timezone.now(), member["id"], request.user.pk],
                )
            messages.success(request, "受講者プロフィールを有効にしました。" if next_active else "受講者プロフィールを無効にしました。")
            return redirect("club:family_member_manage")

        messages.error(request, "操作内容が不正です。")
        return redirect("club:family_member_manage")

    level_choices = _level_choices()
    parent_level = getattr(request.user, "member_level", "") or ""
    parent_row = {
        "full_name": _user_display_name(request.user),
        "relationship_label": "本人",
        "member_level": parent_level,
        "member_level_label": _choice_label(level_choices, parent_level),
        "note": "ログイン中の親アカウントです。チケットは家族共通で管理します。",
    }

    return render(
        request,
        "family/member_manage.html",
        {
            "parent_row": parent_row,
            "family_members": _family_member_rows(request.user),
            "level_choices": level_choices,
            "relationship_choices": RELATIONSHIP_CHOICES,
            "default_level": parent_level or (level_choices[0][0] if level_choices else ""),
            "home_url": reverse("club:home"),
        },
    )
