from django.utils.deprecation import MiddlewareMixin


_CANCEL_POLICY_PATCHED = False


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


class AdminDashboardMenuMiddleware(MiddlewareMixin):
    """
    コーチ・業務委託コーチ・admin 用の共通メニューに、かんたん管理への導線を追加します。

    併せて、2026年7月プレオープン一般レッスンの「最後の1名キャンセル不可」例外を
    views.py 本体を崩さずに適用します。
    """

    shortcut_marker = 'href="/admin-dashboard/"'
    daily_group_marker = '<h2 class="coach-menu-group-title">日常業務</h2>\n                <div class="coach-tabs">'

    def process_request(self, request):
        _patch_preopen_last_cancel_policy()
        return None

    def process_response(self, request, response):
        user = getattr(request, "user", None)
        if not user or not getattr(user, "is_authenticated", False):
            return response

        is_coach_menu_user = (
            getattr(user, "role", "") in ("coach", "contractor_coach")
            or bool(getattr(user, "is_staff", False))
            or bool(getattr(user, "is_superuser", False))
        )
        if not is_coach_menu_user:
            return response

        if getattr(response, "streaming", False):
            return response

        content_type = response.get("Content-Type", "")
        if "text/html" not in content_type:
            return response

        try:
            html = response.content.decode(response.charset or "utf-8")
        except Exception:
            return response

        if self.shortcut_marker in html:
            return response

        if self.daily_group_marker not in html:
            return response

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
