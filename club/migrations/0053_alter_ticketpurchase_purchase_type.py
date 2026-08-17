from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("club", "0052_ticketpurchase_idempotency_key")]

    operations = [
        migrations.AlterField(
            model_name="ticketpurchase",
            name="purchase_type",
            field=models.CharField(
                choices=[
                    ("single", "1枚購入"),
                    ("set4", "4枚セット"),
                    ("event", "イベント用"),
                    ("admin", "管理画面調整"),
                    ("formal_free", "無料謝礼"),
                    ("legacy", "旧データ移行"),
                ],
                default="single",
                max_length=20,
            ),
        ),
    ]
