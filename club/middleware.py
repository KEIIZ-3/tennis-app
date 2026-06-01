from django.utils.deprecation import MiddlewareMixin


class AdminDashboardMenuMiddleware(MiddlewareMixin):
    """
    コーチ・業務委託コーチ・admin 用の共通メニューに、かんたん管理への導線を追加します。

    既存の base.html はかなり大きく、直近で調整済みのため、まずはテンプレート本体を崩さずに
    レンダリング後のHTMLへ安全にショートカットを差し込む方式にしています。
    """

    shortcut_marker = 'href="/admin-dashboard/"'
    daily_group_marker = '<h2 class="coach-menu-group-title">日常業務</h2>\n                <div class="coach-tabs">'

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
