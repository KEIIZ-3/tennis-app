from django.apps import AppConfig


class ClubConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "club"
    verbose_name = "クラブ管理"

    def ready(self):
        from . import runtime_fixes  # noqa: F401
        from . import signals  # noqa: F401

        # 固定レッスンの開催回数変更だけを理由とする自動キャンセルを無効化する。
        # 会員本人キャンセル、雨天中止、固定メンバー解除は従来どおり有効。
