import json
from datetime import date

from django.db import migrations, models
import django.db.models.deletion


META_PREFIX = "__EXPENSE_META__"


def _meta(note):
    text = note or ""
    if not text.startswith(META_PREFIX):
        return {}
    first_line = text.split("\\n", 1)[0]
    try:
        return json.loads(first_line[len(META_PREFIX):].strip() or "{}")
    except Exception:
        return {}


def _month(value):
    try:
        year, month = map(int, str(value).split("-"))
        return date(year, month, 1)
    except (TypeError, ValueError):
        return None


def migrate_accounting_metadata(apps, schema_editor):
    CoachExpense = apps.get_model("club", "CoachExpense")
    RainRefund = apps.get_model("club", "RainRefund")

    for expense in CoachExpense.objects.all().iterator():
        meta = _meta(expense.note)
        changed = []
        if expense.category == "ball":
            start = _month(meta.get("ball_period_start"))
            end = _month(meta.get("ball_period_end"))
            if start and end and start <= end:
                expense.settlement_period_start = start
                expense.settlement_period_end = end
                changed = ["settlement_period_start", "settlement_period_end"]
        if changed:
            expense.save(update_fields=changed)

        status = meta.get("approval_status")
        if expense.category != "court" or status not in ("refund_pending", "refunded"):
            continue
        try:
            debit_id = int(meta.get("rain_refund_debit_coach_id"))
            payer_id = int(meta.get("rain_refund_payer_coach_id"))
        except (TypeError, ValueError):
            continue
        account_kind = meta.get("rain_refund_account_kind") or "other"
        account_coach_id = meta.get("rain_refund_account_coach_id")
        collection_id = meta.get("rain_refund_collection_coach_id")
        RainRefund.objects.update_or_create(
            expense_id=expense.pk,
            defaults={
                "lesson_date": expense.expense_date,
                "lesson_label": meta.get("court_refund_lesson_label", ""),
                "amount": max(int(expense.amount or 0), 0),
                "status": "refunded" if status == "refunded" else "pending",
                "booking_account_kind": account_kind,
                "booking_account_coach_id": account_coach_id or None,
                "booking_account_other": meta.get("rain_refund_account_other", ""),
                "collection_coach_id": collection_id or None,
                "debit_coach_id": debit_id,
                "payer_coach_id": payer_id,
            },
        )


class Migration(migrations.Migration):
    dependencies = [("club", "0049_coachavailability_is_recruitment_closed")]

    operations = [
        migrations.AddField(
            model_name="coachexpense",
            name="settlement_period_end",
            field=models.DateField(blank=True, null=True, verbose_name="精算対象終了月"),
        ),
        migrations.AddField(
            model_name="coachexpense",
            name="settlement_period_start",
            field=models.DateField(blank=True, null=True, verbose_name="精算対象開始月"),
        ),
        migrations.CreateModel(
            name="RainRefund",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("lesson_date", models.DateField()),
                ("lesson_label", models.CharField(blank=True, default="", max_length=255)),
                ("amount", models.PositiveIntegerField(default=0)),
                ("status", models.CharField(choices=[("pending", "返金待ち"), ("refunded", "返金済み")], default="pending", max_length=20)),
                ("booking_account_kind", models.CharField(choices=[("coach", "メインコーチ"), ("other", "その他")], max_length=20)),
                ("booking_account_other", models.CharField(blank=True, default="", max_length=255)),
                ("confirmed_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("availability", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="rain_refunds", to="club.coachavailability")),
                ("booking_account_coach", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="rain_refunds_as_booking_account", to="club.user")),
                ("collection_coach", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="rain_refunds_to_collect", to="club.user")),
                ("confirmed_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="confirmed_rain_refunds", to="club.user")),
                ("debit_coach", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="rain_refund_debits", to="club.user")),
                ("expense", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="rain_refund", to="club.coachexpense")),
                ("payer_coach", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="rain_refund_reimbursements", to="club.user")),
            ],
            options={"ordering": ["lesson_date", "id"]},
        ),
        migrations.RunPython(migrate_accounting_metadata, migrations.RunPython.noop),
    ]
