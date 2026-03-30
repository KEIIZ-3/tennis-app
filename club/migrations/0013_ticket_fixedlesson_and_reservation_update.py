import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("club", "0012_user_full_name_user_is_profile_completed_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="user",
            name="ticket_balance",
            field=models.IntegerField(default=0),
        ),
        migrations.AddField(
            model_name="coachavailability",
            name="lesson_type",
            field=models.CharField(
                choices=[
                    ("group", "一般レッスン（2時間 / 1枚）"),
                    ("private", "プライベートレッスン（1時間 / 2枚）"),
                ],
                default="group",
                max_length=20,
            ),
        ),
        migrations.CreateModel(
            name="FixedLesson",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("title", models.CharField(blank=True, default="", max_length=150)),
                ("lesson_type", models.CharField(
                    choices=[
                        ("group", "一般レッスン（2時間 / 1枚）"),
                        ("private", "プライベートレッスン（1時間 / 2枚）"),
                    ],
                    default="group",
                    max_length=20,
                )),
                ("weekday", models.PositiveSmallIntegerField(
                    choices=[
                        (0, "月"),
                        (1, "火"),
                        (2, "水"),
                        (3, "木"),
                        (4, "金"),
                        (5, "土"),
                        (6, "日"),
                    ]
                )),
                ("start_hour", models.PositiveSmallIntegerField(default=9)),
                ("capacity", models.PositiveIntegerField(default=4)),
                ("weeks_ahead", models.PositiveIntegerField(default=8)),
                ("is_active", models.BooleanField(default=True)),
                ("note", models.CharField(blank=True, max_length=255)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("coach", models.ForeignKey(
                    limit_choices_to={"role": "coach"},
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="fixed_lessons_as_coach",
                    to=settings.AUTH_USER_MODEL,
                )),
                ("court", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="fixed_lessons",
                    to="club.court",
                )),
                ("members", models.ManyToManyField(
                    blank=True,
                    limit_choices_to={"role": "member"},
                    related_name="fixed_lessons",
                    to=settings.AUTH_USER_MODEL,
                )),
            ],
            options={
                "ordering": ["weekday", "start_hour", "id"],
            },
        ),
        migrations.AddField(
            model_name="reservation",
            name="canceled_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="reservation",
            name="cancellation_reason",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
        migrations.AddField(
            model_name="reservation",
            name="fixed_lesson",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="reservations",
                to="club.fixedlesson",
            ),
        ),
        migrations.AddField(
            model_name="reservation",
            name="is_fixed_entry",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="reservation",
            name="lesson_type",
            field=models.CharField(
                choices=[
                    ("group", "一般レッスン（2時間 / 1枚）"),
                    ("private", "プライベートレッスン（1時間 / 2枚）"),
                ],
                default="group",
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="reservation",
            name="ticket_consumed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="reservation",
            name="ticket_refunded_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="reservation",
            name="tickets_used",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AlterField(
            model_name="reservation",
            name="status",
            field=models.CharField(
                choices=[
                    ("active", "予約中"),
                    ("canceled", "キャンセル"),
                    ("rain_canceled", "雨天中止"),
                ],
                default="active",
                max_length=20,
            ),
        ),
        migrations.CreateModel(
            name="TicketLedger",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("change_amount", models.IntegerField()),
                ("balance_after", models.IntegerField()),
                ("reason", models.CharField(
                    choices=[
                        ("purchase_single", "チケット1枚購入"),
                        ("purchase_set4", "4枚セット購入"),
                        ("reservation_use", "通常予約で消費"),
                        ("fixed_use", "固定レッスンで消費"),
                        ("cancel_refund", "キャンセル返却"),
                        ("rain_refund", "雨天中止返却"),
                        ("admin_adjust", "管理画面調整"),
                    ],
                    max_length=30,
                )),
                ("note", models.CharField(blank=True, default="", max_length=255)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("created_by", models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name="created_ticket_ledgers",
                    to=settings.AUTH_USER_MODEL,
                )),
                ("fixed_lesson", models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name="ticket_ledgers",
                    to="club.fixedlesson",
                )),
                ("reservation", models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name="ticket_ledgers",
                    to="club.reservation",
                )),
                ("user", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="ticket_ledgers",
                    to=settings.AUTH_USER_MODEL,
                )),
            ],
            options={
                "ordering": ["-created_at", "-id"],
            },
        ),
    ]
