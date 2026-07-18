from django.apps import AppConfig


class ClubConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "club"
    verbose_name = "クラブ管理"

    def ready(self):
        from . import signals  # noqa
        from .court_type_policy import apply_court_type_policy
        from .preopen_level_policy import apply_preopen_level_policy

        apply_court_type_policy()
        apply_preopen_level_policy()
