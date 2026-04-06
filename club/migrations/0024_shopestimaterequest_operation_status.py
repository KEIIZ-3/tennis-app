from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("club", "0023_shopestimaterequest"),
    ]

    operations = [
        migrations.AddField(
            model_name="shopestimaterequest",
            name="admin_note",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="shopestimaterequest",
            name="handling_status",
            field=models.CharField(
                choices=[
                    ("new", "未対応"),
                    ("checked", "確認済み"),
                    ("ordered", "発注済み"),
                    ("completed", "対応完了"),
                    ("canceled", "キャンセル"),
                ],
                default="new",
                max_length=20,
            ),
        ),
    ]
