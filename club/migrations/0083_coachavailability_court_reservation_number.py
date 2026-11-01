from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("club", "0082_coachavailability_court_fee_override")]

    operations = [
        migrations.AddField(
            model_name="coachavailability",
            name="court_reservation_number",
            field=models.CharField(blank=True, default="", max_length=10),
        ),
    ]
