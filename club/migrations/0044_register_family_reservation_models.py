from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


class Migration(migrations.Migration):
    dependencies = [("club", "0043_add_all_target_level_choice")]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[],
            state_operations=[
                migrations.CreateModel(
                    name="FamilyMember",
                    fields=[
                        ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                        ("full_name", models.CharField(max_length=120, verbose_name="受講者名")),
                        ("kana", models.CharField(blank=True, default="", max_length=120, verbose_name="ふりがな")),
                        ("relationship", models.CharField(choices=[("child", "子供"), ("spouse", "配偶者"), ("parent", "親"), ("other", "その他")], default="child", max_length=30, verbose_name="続柄")),
                        ("birth_date", models.DateField(blank=True, null=True, verbose_name="生年月日")),
                        ("member_level", models.CharField(choices=[("family", "ファミリー"), ("beginner", "初級"), ("beginner_plus", "初中級"), ("intermediate", "中級"), ("intermediate_plus", "中上級"), ("advanced", "上級")], max_length=30, verbose_name="レベル")),
                        ("note", models.TextField(blank=True, default="", verbose_name="メモ")),
                        ("is_active", models.BooleanField(default=True, verbose_name="有効")),
                        ("created_at", models.DateTimeField(default=django.utils.timezone.now, verbose_name="作成日時")),
                        ("updated_at", models.DateTimeField(default=django.utils.timezone.now, verbose_name="更新日時")),
                        ("parent", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="family_member_profiles", to=settings.AUTH_USER_MODEL, verbose_name="親アカウント")),
                    ],
                    options={"ordering": ["parent_id", "-is_active", "full_name", "id"], "verbose_name": "家族受講者プロフィール", "verbose_name_plural": "家族受講者プロフィール", "indexes": [models.Index(fields=["parent", "is_active"], name="family_parent_active_idx")]},
                ),
                migrations.CreateModel(
                    name="ReservationParticipant",
                    fields=[
                        ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                        ("participant_type", models.CharField(default="self", max_length=20, verbose_name="参加者種別")),
                        ("participant_name", models.CharField(max_length=120, verbose_name="参加者名")),
                        ("participant_level", models.CharField(blank=True, default="", max_length=30, verbose_name="参加者レベル")),
                        ("participant_level_label", models.CharField(blank=True, default="", max_length=50, verbose_name="参加者レベル表示")),
                        ("relationship_label", models.CharField(blank=True, default="", max_length=50, verbose_name="続柄表示")),
                        ("created_at", models.DateTimeField(default=django.utils.timezone.now, verbose_name="作成日時")),
                        ("updated_at", models.DateTimeField(default=django.utils.timezone.now, verbose_name="更新日時")),
                        ("family_member", models.ForeignKey(blank=True, db_constraint=False, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="reservation_snapshots", to="club.familymember", verbose_name="家族受講者")),
                        ("parent", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="reservation_participant_snapshots", to=settings.AUTH_USER_MODEL, verbose_name="親アカウント")),
                        ("reservation", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="participant_snapshot", to="club.reservation", verbose_name="予約")),
                    ],
                    options={"ordering": ["-created_at", "-id"], "indexes": [models.Index(fields=["parent", "participant_type"], name="res_part_parent_type_idx"), models.Index(fields=["family_member"], name="res_part_family_idx")]},
                ),
                migrations.CreateModel(
                    name="LessonWaitlistParticipant",
                    fields=[
                        ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                        ("participant_type", models.CharField(default="self", max_length=20, verbose_name="参加者種別")),
                        ("participant_name", models.CharField(max_length=120, verbose_name="参加者名")),
                        ("participant_level", models.CharField(blank=True, default="", max_length=30, verbose_name="参加者レベル")),
                        ("participant_level_label", models.CharField(blank=True, default="", max_length=50, verbose_name="参加者レベル表示")),
                        ("relationship_label", models.CharField(blank=True, default="", max_length=50, verbose_name="続柄表示")),
                        ("created_at", models.DateTimeField(default=django.utils.timezone.now, verbose_name="作成日時")),
                        ("updated_at", models.DateTimeField(default=django.utils.timezone.now, verbose_name="更新日時")),
                        ("family_member", models.ForeignKey(blank=True, db_constraint=False, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="waitlist_snapshots", to="club.familymember", verbose_name="家族受講者")),
                        ("parent", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="lesson_waitlist_participant_snapshots", to=settings.AUTH_USER_MODEL, verbose_name="親アカウント")),
                        ("waitlist", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="participant_snapshot", to="club.lessonwaitlist", verbose_name="キャンセル待ち")),
                    ],
                    options={"ordering": ["-created_at", "-id"], "indexes": [models.Index(fields=["parent", "participant_type"], name="wait_part_parent_type_idx"), models.Index(fields=["family_member"], name="wait_part_family_idx")]},
                ),
            ],
        )
    ]
