# Generated manually for family reservation participant phase 2.
#
# 第2弾では、予約ごとに「実際に参加する人」のスナップショットを保存します。
# models.py の巨大変更を避けるため、Djangoのモデル状態には登録せず、
# DBテーブルだけを作成します。
#
# club_familymember は第1弾でDBテーブルだけ作成しているため、
# ここでは family_member_id を整数カラムとして保持します。

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


class Migration(migrations.Migration):

    dependencies = [
        ("club", "0037_alter_court_court_type"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.CreateModel(
                    name="ReservationParticipant",
                    fields=[
                        ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                        (
                            "reservation",
                            models.OneToOneField(
                                on_delete=django.db.models.deletion.CASCADE,
                                related_name="participant_snapshot",
                                to="club.reservation",
                                verbose_name="予約",
                            ),
                        ),
                        (
                            "parent",
                            models.ForeignKey(
                                on_delete=django.db.models.deletion.CASCADE,
                                related_name="reservation_participant_snapshots",
                                to=settings.AUTH_USER_MODEL,
                                verbose_name="親アカウント",
                            ),
                        ),
                        ("family_member_id", models.PositiveBigIntegerField(blank=True, null=True, verbose_name="家族受講者ID")),
                        ("participant_type", models.CharField(default="self", max_length=20, verbose_name="参加者種別")),
                        ("participant_name", models.CharField(max_length=120, verbose_name="参加者名")),
                        ("participant_level", models.CharField(blank=True, default="", max_length=30, verbose_name="参加者レベル")),
                        ("participant_level_label", models.CharField(blank=True, default="", max_length=50, verbose_name="参加者レベル表示")),
                        ("relationship_label", models.CharField(blank=True, default="", max_length=50, verbose_name="続柄表示")),
                        ("created_at", models.DateTimeField(default=django.utils.timezone.now, verbose_name="作成日時")),
                        ("updated_at", models.DateTimeField(default=django.utils.timezone.now, verbose_name="更新日時")),
                    ],
                    options={
                        "verbose_name": "予約参加者スナップショット",
                        "verbose_name_plural": "予約参加者スナップショット",
                        "ordering": ["-created_at", "-id"],
                    },
                ),
                migrations.AddIndex(
                    model_name="reservationparticipant",
                    index=models.Index(fields=["parent", "participant_type"], name="res_part_parent_type_idx"),
                ),
                migrations.AddIndex(
                    model_name="reservationparticipant",
                    index=models.Index(fields=["family_member_id"], name="res_part_family_idx"),
                ),
            ],
            state_operations=[],
        ),
    ]
