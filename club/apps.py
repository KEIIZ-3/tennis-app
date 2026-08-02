from django.apps import AppConfig


class ClubConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "club"
    verbose_name = "クラブ管理"

    def ready(self):
        from . import runtime_fixes  # noqa: F401
        from . import lesson_calendar_fixes  # noqa: F401
        from . import signals  # noqa: F401

        # 管理サイトの経費編集画面へ、共通経費・個人経費の変更欄を追加する。
        # admin.py の登録完了後にフォームを差し替えるため、ここで明示的に読み込む。
        from . import admin as club_admin  # noqa: F401
        from . import expense_admin_type_editor  # noqa: F401

        # 赤木琴江さんの固定予約状況を、本番データを変更せず起動時に一度だけ診断する。
        # 調査完了後に削除する一時的な読み取り専用ログ。
        from . import akagi_reservation_diagnostic  # noqa: F401
