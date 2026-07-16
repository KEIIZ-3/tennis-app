from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


class Migration(migrations.Migration):

    dependencies = [
        ("club", "0039_lesson_waitlist_participant"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.CreateModel(
                    name="MonthlySettlement",
                    fields=[
                        (
                            "id",
                            models.BigAutoField(
                                auto_created=True,
                                primary_key=True,
                                serialize=False,
                                verbose_name="ID",
                            ),
                        ),
                        ("year", models.PositiveIntegerField(verbose_name="対象年")),
                        ("month", models.PositiveSmallIntegerField(verbose_name="対象月")),
                        (
                            "status",
                            models.CharField(
                                choices=[
                                    ("draft", "編集中"),
                                    ("closed", "締め済み"),
                                ],
                                default="draft",
                                max_length=20,
                                verbose_name="締め状態",
                            ),
                        ),
                        ("opening_balance", models.IntegerField(default=0, verbose_name="前月繰越残高")),
                        ("cash_in_total", models.IntegerField(default=0, verbose_name="当月実入金")),
                        ("cash_out_total", models.IntegerField(default=0, verbose_name="当月実支出")),
                        ("closing_balance", models.IntegerField(default=0, verbose_name="当月末残高")),
                        ("ticket_cash_in", models.IntegerField(default=0, verbose_name="チケット販売入金")),
                        ("preopen_cash_in", models.IntegerField(default=0, verbose_name="プレオープン参加費入金")),
                        ("stringing_cash_in", models.IntegerField(default=0, verbose_name="ガット張り入金")),
                        ("other_cash_in", models.IntegerField(default=0, verbose_name="その他入金")),
                        ("salary_cash_out", models.IntegerField(default=0, verbose_name="給与支払")),
                        ("reimbursement_cash_out", models.IntegerField(default=0, verbose_name="立替精算支払")),
                        ("common_expense_cash_out", models.IntegerField(default=0, verbose_name="共通経費支出")),
                        ("contractor_cash_out", models.IntegerField(default=0, verbose_name="業務委託給与支出")),
                        ("other_cash_out", models.IntegerField(default=0, verbose_name="その他支出")),
                        ("unpaid_salary_total", models.IntegerField(default=0, verbose_name="未払給与合計")),
                        ("unpaid_reimbursement_total", models.IntegerField(default=0, verbose_name="未精算立替合計")),
                        ("uncollected_revenue_total", models.IntegerField(default=0, verbose_name="未回収売上合計")),
                        (
                            "calculation_snapshot",
                            models.JSONField(
                                blank=True,
                                default=dict,
                                verbose_name="締め時計算スナップショット",
                            ),
                        ),
                        ("note", models.TextField(blank=True, default="", verbose_name="メモ")),
                        ("closed_at", models.DateTimeField(blank=True, null=True, verbose_name="締め日時")),
                        ("reopened_at", models.DateTimeField(blank=True, null=True, verbose_name="締め解除日時")),
                        (
                            "created_at",
                            models.DateTimeField(
                                default=django.utils.timezone.now,
                                verbose_name="作成日時",
                            ),
                        ),
                        (
                            "updated_at",
                            models.DateTimeField(
                                default=django.utils.timezone.now,
                                verbose_name="更新日時",
                            ),
                        ),
                        (
                            "closed_by",
                            models.ForeignKey(
                                blank=True,
                                null=True,
                                on_delete=django.db.models.deletion.SET_NULL,
                                related_name="closed_monthly_settlements",
                                to=settings.AUTH_USER_MODEL,
                                verbose_name="締め担当者",
                            ),
                        ),
                        (
                            "reopened_by",
                            models.ForeignKey(
                                blank=True,
                                null=True,
                                on_delete=django.db.models.deletion.SET_NULL,
                                related_name="reopened_monthly_settlements",
                                to=settings.AUTH_USER_MODEL,
                                verbose_name="締め解除担当者",
                            ),
                        ),
                    ],
                    options={
                        "db_table": "club_monthlysettlement",
                        "ordering": ["-year", "-month"],
                    },
                ),
                migrations.AddConstraint(
                    model_name="monthlysettlement",
                    constraint=models.UniqueConstraint(
                        fields=("year", "month"),
                        name="club_monthly_settlement_year_month_uniq",
                    ),
                ),
                migrations.AddConstraint(
                    model_name="monthlysettlement",
                    constraint=models.CheckConstraint(
                        condition=models.Q(("month__gte", 1), ("month__lte", 12)),
                        name="club_monthly_settlement_month_range",
                    ),
                ),
                migrations.AddIndex(
                    model_name="monthlysettlement",
                    index=models.Index(
                        fields=["status", "year", "month"],
                        name="club_month_status_idx",
                    ),
                ),
                migrations.CreateModel(
                    name="CoachMonthlySettlement",
                    fields=[
                        (
                            "id",
                            models.BigAutoField(
                                auto_created=True,
                                primary_key=True,
                                serialize=False,
                                verbose_name="ID",
                            ),
                        ),
                        ("is_contractor_coach", models.BooleanField(default=False, verbose_name="業務委託コーチ")),
                        ("lesson_count", models.PositiveIntegerField(default=0, verbose_name="担当レッスン数")),
                        ("ticket_revenue", models.IntegerField(default=0, verbose_name="チケット売上配分")),
                        ("preopen_paid_revenue", models.IntegerField(default=0, verbose_name="プレオープン回収済売上配分")),
                        ("preopen_unpaid_revenue", models.IntegerField(default=0, verbose_name="プレオープン未回収売上配分")),
                        ("stringing_revenue", models.IntegerField(default=0, verbose_name="ガット張り売上")),
                        ("contractor_work_amount", models.IntegerField(default=0, verbose_name="業務委託給与")),
                        ("common_expense_share", models.IntegerField(default=0, verbose_name="共通経費負担")),
                        ("reimbursement_carry_in", models.IntegerField(default=0, verbose_name="前月以前の未精算立替")),
                        ("reimbursement_current_month", models.IntegerField(default=0, verbose_name="当月承認立替")),
                        ("reimbursement_due", models.IntegerField(default=0, verbose_name="立替精算対象額")),
                        ("salary_due", models.IntegerField(default=0, verbose_name="給与確定額")),
                        ("salary_paid", models.IntegerField(default=0, verbose_name="給与支払済額")),
                        ("salary_unpaid", models.IntegerField(default=0, verbose_name="給与未払額")),
                        ("reimbursement_paid", models.IntegerField(default=0, verbose_name="立替精算支払済額")),
                        ("reimbursement_unpaid", models.IntegerField(default=0, verbose_name="立替未精算額")),
                        (
                            "calculation_snapshot",
                            models.JSONField(
                                blank=True,
                                default=dict,
                                verbose_name="コーチ別計算スナップショット",
                            ),
                        ),
                        (
                            "created_at",
                            models.DateTimeField(
                                default=django.utils.timezone.now,
                                verbose_name="作成日時",
                            ),
                        ),
                        (
                            "updated_at",
                            models.DateTimeField(
                                default=django.utils.timezone.now,
                                verbose_name="更新日時",
                            ),
                        ),
                        (
                            "coach",
                            models.ForeignKey(
                                on_delete=django.db.models.deletion.PROTECT,
                                related_name="monthly_settlement_rows",
                                to=settings.AUTH_USER_MODEL,
                                verbose_name="コーチ",
                            ),
                        ),
                        (
                            "monthly_settlement",
                            models.ForeignKey(
                                on_delete=django.db.models.deletion.CASCADE,
                                related_name="coach_settlements",
                                to="club.monthlysettlement",
                                verbose_name="月次精算",
                            ),
                        ),
                    ],
                    options={
                        "db_table": "club_coachmonthlysettlement",
                        "ordering": ["monthly_settlement_id", "coach_id"],
                    },
                ),
                migrations.AddConstraint(
                    model_name="coachmonthlysettlement",
                    constraint=models.UniqueConstraint(
                        fields=("monthly_settlement", "coach"),
                        name="club_coach_month_settlement_uniq",
                    ),
                ),
                migrations.AddIndex(
                    model_name="coachmonthlysettlement",
                    index=models.Index(
                        fields=["coach", "monthly_settlement"],
                        name="club_coach_month_idx",
                    ),
                ),
                migrations.CreateModel(
                    name="SettlementPayment",
                    fields=[
                        (
                            "id",
                            models.BigAutoField(
                                auto_created=True,
                                primary_key=True,
                                serialize=False,
                                verbose_name="ID",
                            ),
                        ),
                        (
                            "payment_type",
                            models.CharField(
                                choices=[
                                    ("salary", "給与支払い"),
                                    ("reimbursement", "本人立替精算支払い"),
                                ],
                                max_length=30,
                                verbose_name="支払種別",
                            ),
                        ),
                        ("amount", models.PositiveIntegerField(verbose_name="支払金額")),
                        (
                            "paid_date",
                            models.DateField(
                                default=django.utils.timezone.localdate,
                                verbose_name="支払日",
                            ),
                        ),
                        ("note", models.TextField(blank=True, default="", verbose_name="メモ")),
                        (
                            "legacy_coach_expense_id",
                            models.PositiveBigIntegerField(
                                blank=True,
                                null=True,
                                verbose_name="既存支払記録ID",
                            ),
                        ),
                        ("is_reversed", models.BooleanField(default=False, verbose_name="取消済み")),
                        ("reversed_at", models.DateTimeField(blank=True, null=True, verbose_name="取消日時")),
                        ("reversal_note", models.TextField(blank=True, default="", verbose_name="取消理由")),
                        (
                            "created_at",
                            models.DateTimeField(
                                default=django.utils.timezone.now,
                                verbose_name="作成日時",
                            ),
                        ),
                        (
                            "updated_at",
                            models.DateTimeField(
                                default=django.utils.timezone.now,
                                verbose_name="更新日時",
                            ),
                        ),
                        (
                            "coach",
                            models.ForeignKey(
                                on_delete=django.db.models.deletion.PROTECT,
                                related_name="settlement_payments",
                                to=settings.AUTH_USER_MODEL,
                                verbose_name="支払先コーチ",
                            ),
                        ),
                        (
                            "created_by",
                            models.ForeignKey(
                                blank=True,
                                null=True,
                                on_delete=django.db.models.deletion.SET_NULL,
                                related_name="created_settlement_payments",
                                to=settings.AUTH_USER_MODEL,
                                verbose_name="登録者",
                            ),
                        ),
                        (
                            "monthly_settlement",
                            models.ForeignKey(
                                on_delete=django.db.models.deletion.PROTECT,
                                related_name="payments",
                                to="club.monthlysettlement",
                                verbose_name="計上月次精算",
                            ),
                        ),
                        (
                            "reversed_by",
                            models.ForeignKey(
                                blank=True,
                                null=True,
                                on_delete=django.db.models.deletion.SET_NULL,
                                related_name="reversed_settlement_payments",
                                to=settings.AUTH_USER_MODEL,
                                verbose_name="取消担当者",
                            ),
                        ),
                    ],
                    options={
                        "db_table": "club_settlementpayment",
                        "ordering": ["paid_date", "id"],
                    },
                ),
                migrations.AddConstraint(
                    model_name="settlementpayment",
                    constraint=models.CheckConstraint(
                        condition=models.Q(("amount__gt", 0)),
                        name="club_settlement_payment_amount_gt_zero",
                    ),
                ),
                migrations.AddIndex(
                    model_name="settlementpayment",
                    index=models.Index(
                        fields=["coach", "payment_type", "paid_date"],
                        name="club_payment_coach_type_idx",
                    ),
                ),
                migrations.AddIndex(
                    model_name="settlementpayment",
                    index=models.Index(
                        fields=["monthly_settlement", "is_reversed"],
                        name="club_payment_month_active_idx",
                    ),
                ),
                migrations.CreateModel(
                    name="ExpenseSettlementAllocation",
                    fields=[
                        (
                            "id",
                            models.BigAutoField(
                                auto_created=True,
                                primary_key=True,
                                serialize=False,
                                verbose_name="ID",
                            ),
                        ),
                        ("amount", models.PositiveIntegerField(verbose_name="充当金額")),
                        ("allocation_order", models.PositiveIntegerField(default=1, verbose_name="充当順")),
                        (
                            "created_at",
                            models.DateTimeField(
                                default=django.utils.timezone.now,
                                verbose_name="作成日時",
                            ),
                        ),
                        (
                            "expense",
                            models.ForeignKey(
                                on_delete=django.db.models.deletion.PROTECT,
                                related_name="settlement_allocations",
                                to="club.coachexpense",
                                verbose_name="対象経費",
                            ),
                        ),
                        (
                            "payment",
                            models.ForeignKey(
                                on_delete=django.db.models.deletion.CASCADE,
                                related_name="expense_allocations",
                                to="club.settlementpayment",
                                verbose_name="立替精算支払",
                            ),
                        ),
                    ],
                    options={
                        "db_table": "club_expensesettlementallocation",
                        "ordering": ["payment_id", "allocation_order", "expense_id"],
                    },
                ),
                migrations.AddConstraint(
                    model_name="expensesettlementallocation",
                    constraint=models.UniqueConstraint(
                        fields=("payment", "expense"),
                        name="club_payment_expense_allocation_uniq",
                    ),
                ),
                migrations.AddConstraint(
                    model_name="expensesettlementallocation",
                    constraint=models.CheckConstraint(
                        condition=models.Q(("amount__gt", 0)),
                        name="club_expense_allocation_amount_gt_zero",
                    ),
                ),
                migrations.AddIndex(
                    model_name="expensesettlementallocation",
                    index=models.Index(
                        fields=["expense", "payment"],
                        name="club_expense_payment_idx",
                    ),
                ),
            ],
            state_operations=[],
        ),
    ]
