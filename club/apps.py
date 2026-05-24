from django.apps import AppConfig


class ClubConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "club"
    verbose_name = "クラブ管理"

    def ready(self):
        from . import signals  # noqa
