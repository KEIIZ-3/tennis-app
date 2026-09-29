from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("club", "0076_courtnumbernoticehistory")]

    operations = [
        migrations.AddField(
            model_name="coachavailability",
            name="group_tickets_per_person_per_hour",
            field=models.PositiveSmallIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="reservation",
            name="group_tickets_per_person_per_hour",
            field=models.PositiveSmallIntegerField(blank=True, null=True),
        ),
    ]
