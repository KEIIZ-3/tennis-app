import re

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.http import HttpResponse
from django.middleware.csrf import get_token
from django.shortcuts import get_object_or_404, redirect
from django.utils.html import escape
from django.views.decorators.http import require_POST

from . import family_members, views
from .models import FamilyMember, Reservation


_LAST_MEMBER_MESSAGE = "最後の1名となるため、この予約はキャンセルできません。"


def _can_cancel_reservation(user, reservation):
    if not user or not getattr(user, "is_authenticated", False):
        return False
    if reservation.user_id == getattr(user, "pk", None):
        return True
    if getattr(user, "is_staff", False) or getattr(user, "is_superuser", False):
        return True
    return getattr(user, "role", "") in ("coach", "contractor_coach")


def _replace_last_member_cancel_message(request, response):
    """旧判定で残る「最後の1名はキャンセル不可」表示を正式な取消ボタンへ置換する。"""
    content_type = response.get("Content-Type", "")
    if response.status_code != 200 or "text/html" not in content_type:
        return response

    try:
        html = response.content.decode(response.charset or "utf-8")
    except Exception:
        return response

    if _LAST_MEMBER_MESSAGE not in html:
        return response

    csrf_token = escape(get_token(request))
    pattern = re.compile(
        r'(<a href="(?P<detail>/reservations/(?P<pk>\d+)/)" class="btn btn-primary">詳細を見る</a>)'
        r'<button type="button" class="btn" disabled>'
        + re.escape(_LAST_MEMBER_MESSAGE)
        + r'</button>'
    )

    def replacement(match):
        pk = match.group("pk")
        return (
            match.group(1)
            + f'<form method="post" action="/reservations/{pk}/cancel/">'
            + f'<input type="hidden" name="csrfmiddlewaretoken" value="{csrf_token}">'
            + '<button type="submit" class="btn-danger" '
            + 'onclick="return confirm(\'この予約をキャンセルしますか？\');">キャンセル</button>'
            + "</form>"
        )

    updated_html = pattern.sub(replacement, html)
    if updated_html == html:
        return response

    response.content = updated_html.encode(response.charset or "utf-8")
    if response.has_header("Content-Length"):
        response["Content-Length"] = str(len(response.content))
    return response


def _inject_family_delete_buttons(request, response):
    """家族プロフィール一覧へ、本人所有データだけを削除できるボタンを追加する。"""
    content_type = response.get("Content-Type", "")
    if response.status_code != 200 or "text/html" not in content_type:
        return response

    try:
        html = response.content.decode(response.charset or "utf-8")
    except Exception:
        return response

    if '<div class="mini-actions">' not in html:
        return response

    csrf_token = escape(get_token(request))
    pattern = re.compile(
        r'(?P<block><div class="mini-actions">\s*<form method="post">.*?'
        r'<input type="hidden" name="member_id" value="(?P<member_id>\d+)">.*?</form>)'
        r'(?P<close>\s*</div>)',
        re.DOTALL,
    )

    def replacement(match):
        member_id = match.group("member_id")
        delete_form = (
            '<form method="post">'
            f'<input type="hidden" name="csrfmiddlewaretoken" value="{csrf_token}">'
            '<input type="hidden" name="action" value="delete">'
            f'<input type="hidden" name="member_id" value="{member_id}">'
            '<button type="submit" class="btn-danger" '
            'onclick="return confirm(\'この家族受講者を削除しますか？過去の予約履歴に保存された参加者名は残ります。\');">削除する</button>'
            '</form>'
        )
        return match.group("block") + delete_form + match.group("close")

    updated_html = pattern.sub(replacement, html)
    if updated_html == html:
        return response

    response.content = updated_html.encode(response.charset or "utf-8")
    if response.has_header("Content-Length"):
        response["Content-Length"] = str(len(response.content))
    return response


@login_required
def reservation_list(request):
    response = views.reservation_list(request)
    return _replace_last_member_cancel_message(request, response)


@login_required
def family_member_manage(request):
    if request.method == "POST" and (request.POST.get("action") or "").strip() == "delete":
        member = FamilyMember.objects.filter(
            pk=request.POST.get("member_id"),
            parent=request.user,
        ).first()
        if not member:
            messages.error(request, "対象の家族受講者プロフィールが見つかりません。")
            return redirect("club:family_member_manage")

        member_name = member.full_name
        member.delete()
        messages.success(request, f"{member_name}さんの家族受講者プロフィールを削除しました。")
        return redirect("club:family_member_manage")

    response = family_members.family_member_manage(request)
    return _inject_family_delete_buttons(request, response)


@login_required
@require_POST
def reservation_cancel(request, pk):
    """参加者が最後の1名の場合でも予約キャンセルを許可する。"""
    reservation = get_object_or_404(
        Reservation.objects.select_related(
            "user",
            "coach",
            "substitute_coach",
            "court",
            "availability",
            "fixed_lesson",
        ),
        pk=pk,
    )

    if not _can_cancel_reservation(request.user, reservation):
        return HttpResponse("Forbidden", status=403)

    if reservation.status not in (
        Reservation.STATUS_ACTIVE,
        Reservation.STATUS_PENDING,
    ):
        messages.info(request, "この予約はすでにキャンセル済み、またはキャンセルできない状態です。")
        return redirect("club:reservation_detail", pk=reservation.pk)

    try:
        canceled = reservation.cancel(
            created_by=request.user,
            reason="会員キャンセル" if reservation.user_id == request.user.pk else "コーチ・管理者キャンセル",
        )
        if canceled:
            messages.success(request, "予約をキャンセルしました。消費済みチケットは返却しました。")
        else:
            messages.info(request, "予約状態に変更はありませんでした。")
    except ValidationError as exc:
        for message in getattr(exc, "messages", [str(exc)]):
            messages.error(request, message)
    except Exception as exc:
        messages.error(request, f"予約のキャンセルに失敗しました: {exc}")

    return redirect("club:reservation_detail", pk=reservation.pk)
