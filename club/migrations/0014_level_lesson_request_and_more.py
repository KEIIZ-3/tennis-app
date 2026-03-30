import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("club", "0013_ticket_fixedlesson_and_reservation_update"),
    ]

    operations = [
        migrations.AddField(
            model_name="user",
            name="member_level",
            field=models.CharField(
                choices=[
                    ("family", "ファミリー"),
                    ("beginner", "初級"),
                    ("beginner_plus", "初中級"),
                    ("intermediate", "中級"),
                    ("intermediate_plus", "中上級"),
                    ("advanced", "上級"),
                ],
                default="beginner",
                max_length=30,
            ),
        ),
        migrations.AddField(
            model_name="coachavailability",
            name="custom_duration_hours",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="coachavailability",
            name="custom_ticket_price",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="coachavailability",
            name="status",
            field=models.CharField(
                choices=[
                    ("open", "公開中"),
                    ("requested", "申請中"),
                    ("approved", "承認済み"),
                ],
                default="open",
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="coachavailability",
            name="target_level",
            field=models.CharField(
                choices=[
                    ("family", "ファミリー"),
                    ("beginner", "初級"),
                    ("beginner_plus", "初中級"),
                    ("intermediate", "中級"),
                    ("intermediate_plus", "中上級"),
                    ("advanced", "上級"),
                ],
                default="beginner",
                max_length=30,
            ),
        ),
        migrations.AddField(
            model_name="fixedlesson",
            name="target_level",
            field=models.CharField(
                choices=[
                    ("family", "ファミリー"),
                    ("beginner", "初級"),
                    ("beginner_plus", "初中級"),
                    ("intermediate", "中級"),
                    ("intermediate_plus", "中上級"),
                    ("advanced", "上級"),
                ],
                default="beginner",
                max_length=30,
            ),
        ),
        migrations.AddField(
            model_name="reservation",
            name="approved_court_note",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
        migrations.AddField(
            model_name="reservation",
            name="custom_duration_hours",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="reservation",
            name="custom_ticket_price",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="reservation",
            name="requested_court_note",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
        migrations.AddField(
            model_name="reservation",
            name="requested_court_type",
            field=models.CharField(
                choices=[
                    ("sono", "西猪名公園テニスコート"),
                    ("other", "それ以外のコート"),
                ],
                default="sono",
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="reservation",
            name="target_level",
            field=models.CharField(
                choices=[
                    ("family", "ファミリー"),
                    ("beginner", "初級"),
                    ("beginner_plus", "初中級"),
                    ("intermediate", "中級"),
                    ("intermediate_plus", "中上級"),
                    ("advanced", "上級"),
                ],
                default="beginner",
                max_length=30,
            ),
        ),
        migrations.AlterField(
            model_name="court",
            name="court_type",
            field=models.CharField(
                choices=[
                    ("sono", "西猪名公園テニスコート"),
                    ("other", "それ以外のコート"),
                ],
                default="sono",
                max_length=20,
            ),
        ),
        migrations.AlterField(
            model_name="coachavailability",
            name="lesson_type",
            field=models.CharField(
                choices=[
                    ("general", "一般レッスン"),
                    ("private", "プライベートレッスン"),
                    ("group", "グループレッスン"),
                    ("event", "イベント"),
                ],
                default="general",
                max_length=20,
            ),
        ),
        migrations.AlterField(
            model_name="fixedlesson",
            name="lesson_type",
            field=models.CharField(
                choices=[
                    ("general", "一般レッスン"),
                    ("private", "プライベートレッスン"),
                    ("group", "グループレッスン"),
                    ("event", "イベント"),
                ],
                default="general",
                max_length=20,
            ),
        ),
        migrations.AlterField(
            model_name="reservation",
            name="lesson_type",
            field=models.CharField(
                choices=[
                    ("general", "一般レッスン"),
                    ("private", "プライベートレッスン"),
                    ("group", "グループレッスン"),
                    ("event", "イベント"),
                ],
                default="general",
                max_length=20,
            ),
        ),
        migrations.AlterField(
            model_name="reservation",
            name="status",
            field=models.CharField(
                choices=[
                    ("active", "予約中"),
                    ("canceled", "キャンセル"),
                    ("rain_canceled", "雨天中止"),
                    ("pending", "承認待ち"),
                ],
                default="active",
                max_length=20,
            ),
        ),
    ]
