# Generated manually for tennis-app

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("club", "0030_lesson_waitlist"),
    ]

    operations = [
        migrations.AddField(
            model_name="reservation",
            name="payment_method",
            field=models.CharField(
                choices=[
                    ("ticket", "チケット"),
                    ("cash", "当日受付"),
                    ("other", "その他"),
                ],
                default="ticket",
                max_length=20,
                verbose_name="支払方法",
            ),
        ),
        migrations.AddField(
            model_name="reservation",
            name="payment_status",
            field=models.CharField(
                choices=[
                    ("not_required", "対象外"),
                    ("unpaid", "未回収"),
                    ("paid", "回収済み"),
                    ("waived", "免除"),
                ],
                default="not_required",
                max_length=20,
                verbose_name="支払状況",
            ),
        ),
        migrations.AddField(
            model_name="reservation",
            name="payment_amount",
            field=models.PositiveIntegerField(default=0, verbose_name="参加費"),
        ),
        migrations.AddField(
            model_name="reservation",
            name="payment_received_at",
            field=models.DateTimeField(blank=True, null=True, verbose_name="回収日時"),
        ),
        migrations.AddField(
            model_name="reservation",
            name="payment_note",
            field=models.CharField(blank=True, default="", max_length=255, verbose_name="支払メモ"),
        ),
    ]
