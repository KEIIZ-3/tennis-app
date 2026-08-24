from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("club", "0060_ticket_purchase_reversal"),
    ]
    operations = [
        migrations.CreateModel(
            name="TicketBurdenChange",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("tickets", models.PositiveIntegerField()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("created_by", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="created_ticket_burden_changes", to=settings.AUTH_USER_MODEL)),
                ("new_payer", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="ticket_burden_changes_to", to=settings.AUTH_USER_MODEL)),
                ("previous_payer", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="ticket_burden_changes_from", to=settings.AUTH_USER_MODEL)),
                ("reservation", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="ticket_burden_changes", to="club.reservation")),
            ],
            options={"ordering": ["created_at", "id"]},
        ),
    ]
