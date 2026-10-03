from django.db import migrations, models
import django.db.models.deletion
from django.conf import settings


class Migration(migrations.Migration):
    dependencies = [("club", "0080_rainrefund_nullable_payer_coach")]

    operations = [
        migrations.AddField(
            model_name="coachavailability", name="court_payer_kind",
            field=models.CharField(blank=True, choices=[("", "未設定"), ("company_wallet", "会社の財布"), ("coach", "コーチ")], default="", max_length=20),
        ),
        migrations.AddField(
            model_name="coachavailability", name="court_payer_coach",
            field=models.ForeignKey(blank=True, limit_choices_to={"role__in": ("coach", "contractor_coach")}, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="court_payer_availabilities", to=settings.AUTH_USER_MODEL),
        ),
        migrations.AddField(
            model_name="coachavailability", name="court_booking_account_kind",
            field=models.CharField(blank=True, choices=[("", "未設定"), ("coach", "コーチ"), ("other", "その他")], default="", max_length=20),
        ),
        migrations.AddField(
            model_name="coachavailability", name="court_booking_account_coach",
            field=models.ForeignKey(blank=True, limit_choices_to={"role__in": ("coach", "contractor_coach")}, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="court_booking_account_availabilities", to=settings.AUTH_USER_MODEL),
        ),
        migrations.AddField(
            model_name="coachavailability", name="court_booking_account_other",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
    ]
