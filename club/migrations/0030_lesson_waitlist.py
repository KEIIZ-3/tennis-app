# Generated manually for tennis-app

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("club", "0029_fixedlesson_start_date"),
    ]

    operations = [
        migrations.CreateModel(
            name="LessonWaitlist",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("lesson_type", models.CharField(choices=[("general", "一般レッスン"), ("private", "プライベートレッスン"), ("group", "グループレッスン"), ("event", "イベント")], default="general", max_length=20, verbose_name="レッスン種別")),
                ("target_level", models.CharField(choices=[("family", "ファミリー"), ("beginner", "初級"), ("beginner_plus", "初中級"), ("intermediate", "中級"), ("intermediate_plus", "中上級"), ("advanced", "上級")], default="beginner", max_length=30, verbose_name="対象レベル")),
                ("start_at", models.DateTimeField(verbose_name="開始日時")),
                ("end_at", models.DateTimeField(verbose_name="終了日時")),
                ("status", models.CharField(choices=[("waiting", "キャンセル待ち中"), ("canceled", "キャンセル"), ("converted", "予約済みに変更")], default="waiting", max_length=20, verbose_name="状態")),
                ("note", models.CharField(blank=True, default="", max_length=255, verbose_name="メモ")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="登録日時")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="更新日時")),
                ("canceled_at", models.DateTimeField(blank=True, null=True, verbose_name="キャンセル日時")),
                ("converted_at", models.DateTimeField(blank=True, null=True, verbose_name="予約変更日時")),
                ("availability", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="lesson_waitlists", to="club.coachavailability", verbose_name="レッスン枠")),
                ("coach", models.ForeignKey(limit_choices_to={"role": "coach"}, on_delete=django.db.models.deletion.CASCADE, related_name="coach_lesson_waitlists", to=settings.AUTH_USER_MODEL, verbose_name="主担当コーチ")),
                ("court", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="lesson_waitlists", to="club.court", verbose_name="コート")),
                ("fixed_lesson", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="lesson_waitlists", to="club.fixedlesson", verbose_name="固定レッスン")),
                ("substitute_coach", models.ForeignKey(blank=True, limit_choices_to={"role": "coach"}, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="substitute_lesson_waitlists", to=settings.AUTH_USER_MODEL, verbose_name="代行コーチ")),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="lesson_waitlists", to=settings.AUTH_USER_MODEL, verbose_name="会員")),
            ],
            options={
                "verbose_name": "キャンセル待ち",
                "verbose_name_plural": "キャンセル待ち",
                "ordering": ["start_at", "created_at", "id"],
            },
        ),
        migrations.AddConstraint(
            model_name="lessonwaitlist",
            constraint=models.UniqueConstraint(condition=models.Q(status="waiting"), fields=("user", "coach", "court", "lesson_type", "start_at", "end_at"), name="unique_waiting_lesson_per_user_slot"),
        ),
    ]
