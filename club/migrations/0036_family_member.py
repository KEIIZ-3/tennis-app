# Generated manually for family member profile phase 1.

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


class Migration(migrations.Migration):

    dependencies = [
        ("club", "0035_alter_fixedlesson_members"),
    ]

    operations = [
        migrations.CreateModel(
            name="FamilyMember",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("full_name", models.CharField(max_length=120, verbose_name="受講者名")),
                ("kana", models.CharField(blank=True, default="", max_length=120, verbose_name="ふりがな")),
                ("relationship", models.CharField(default="child", max_length=30, verbose_name="続柄")),
                ("birth_date", models.DateField(blank=True, null=True, verbose_name="生年月日")),
                ("member_level", models.CharField(max_length=30, verbose_name="レベル")),
                ("note", models.TextField(blank=True, default="", verbose_name="メモ")),
                ("is_active", models.BooleanField(default=True, verbose_name="有効")),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now, verbose_name="作成日時")),
                ("updated_at", models.DateTimeField(default=django.utils.timezone.now, verbose_name="更新日時")),
                (
                    "parent",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="family_members",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="親アカウント",
                    ),
                ),
            ],
            options={
                "verbose_name": "家族受講者プロフィール",
                "verbose_name_plural": "家族受講者プロフィール",
                "ordering": ["parent_id", "-is_active", "full_name", "id"],
            },
        ),
        migrations.AddIndex(
            model_name="familymember",
            index=models.Index(fields=["parent", "is_active"], name="family_parent_active_idx"),
        ),
    ]
