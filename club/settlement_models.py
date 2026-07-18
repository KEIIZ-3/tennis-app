from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Sum
from django.utils import timezone


class MonthlySettlement(models.Model):
    STATUS_DRAFT = "draft"
    STATUS_CLOSED = "closed"

    STATUS_CHOICES = (
        (STATUS_DRAFT, "編集中"),
        (STATUS_CLOSED, "締め済み"),
    )

    year = models.PositiveIntegerField(verbose_name="対象年")
    month = models.PositiveSmallIntegerField(verbose_name="対象月")
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default=STATUS_DRAFT,
        verbose_name="締め状態",
    )

    opening_balance = models.IntegerField(default=0, verbose_name="前月繰越残高")
    cash_in_total = models.IntegerField(default=0, verbose_name="当月実入金")
    cash_out_total = models.IntegerField(default=0, verbose_name="当月実支出")
    closing_balance = models.IntegerField(default=0, verbose_name="当月末残高")

    ticket_cash_in = models.IntegerField(default=0, verbose_name="チケット販売入金")
    preopen_cash_in = models.IntegerField(default=0, verbose_name="プレオープン参加費入金")
    stringing_cash_in = models.IntegerField(default=0, verbose_name="ガット張り入金")
    other_cash_in = models.IntegerField(default=0, verbose_name="その他入金")

    salary_cash_out = models.IntegerField(default=0, verbose_name="給与支払")
    reimbursement_cash_out = models.IntegerField(default=0, verbose_name="立替精算支払")
    common_expense_cash_out = models.IntegerField(default=0, verbose_name="共通経費支出")
    contractor_cash_out = models.IntegerField(default=0, verbose_name="業務委託給与支出")
    other_cash_out = models.IntegerField(default=0, verbose_name="その他支出")

    unpaid_salary_total = models.IntegerField(default=0, verbose_name="未払給与合計")
    unpaid_reimbursement_total = models.IntegerField(default=0, verbose_name="未精算立替合計")
    uncollected_revenue_total = models.IntegerField(default=0, verbose_name="未回収売上合計")

    calculation_snapshot = models.JSONField(
        default=dict,
        blank=True,
        verbose_name="締め時計算スナップショット",
    )
    note = models.TextField(blank=True, default="", verbose_name="メモ")

    closed_at = models.DateTimeField(null=True, blank=True, verbose_name="締め日時")
    closed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="closed_monthly_settlements",
        verbose_name="締め担当者",
    )
    reopened_at = models.DateTimeField(null=True, blank=True, verbose_name="締め解除日時")
    reopened_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reopened_monthly_settlements",
        verbose_name="締め解除担当者",
    )

    created_at = models.DateTimeField(default=timezone.now, verbose_name="作成日時")
    updated_at = models.DateTimeField(default=timezone.now, verbose_name="更新日時")

    class Meta:
        managed = False
        db_table = "club_monthlysettlement"
        ordering = ["-year", "-month"]
        verbose_name = "月次精算"
        verbose_name_plural = "月次精算"

    def __str__(self):
        return f"{self.year}年{self.month}月 / {self.get_status_display()}"

    def clean(self):
        if self.year < 2000 or self.year > 2100:
            raise ValidationError("対象年は2000〜2100年で指定してください。")
        if self.month < 1 or self.month > 12:
            raise ValidationError("対象月は1〜12で指定してください。")

    @property
    def is_closed(self):
        return self.status == self.STATUS_CLOSED

    def recalculate_closing_balance(self):
        self.closing_balance = (
            int(self.opening_balance or 0)
            + int(self.cash_in_total or 0)
            - int(self.cash_out_total or 0)
        )
        return self.closing_balance

    def close(self, *, user, snapshot=None):
        if self.is_closed:
            return
        self.recalculate_closing_balance()
        self.status = self.STATUS_CLOSED
        self.closed_at = timezone.now()
        self.closed_by = user
        self.reopened_at = None
        self.reopened_by = None
        if snapshot is not None:
            self.calculation_snapshot = snapshot
        self.updated_at = timezone.now()
        self.full_clean()
        self.save()

    def reopen(self, *, user):
        if not self.is_closed:
            return
        self.status = self.STATUS_DRAFT
        self.reopened_at = timezone.now()
        self.reopened_by = user
        self.updated_at = timezone.now()
        self.save(
            update_fields=[
                "status",
                "reopened_at",
                "reopened_by",
                "updated_at",
            ]
        )


class CoachMonthlySettlement(models.Model):
    monthly_settlement = models.ForeignKey(
        MonthlySettlement,
        on_delete=models.CASCADE,
        related_name="coach_settlements",
        verbose_name="月次精算",
    )
    coach = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="monthly_settlement_rows",
        verbose_name="コーチ",
    )

    is_contractor_coach = models.BooleanField(
        default=False,
        verbose_name="業務委託コーチ",
    )
    lesson_count = models.PositiveIntegerField(default=0, verbose_name="担当レッスン数")

    ticket_revenue = models.IntegerField(default=0, verbose_name="チケット売上配分")
    preopen_paid_revenue = models.IntegerField(default=0, verbose_name="プレオープン回収済売上配分")
    preopen_unpaid_revenue = models.IntegerField(default=0, verbose_name="プレオープン未回収売上配分")
    stringing_revenue = models.IntegerField(default=0, verbose_name="ガット張り売上")
    contractor_work_amount = models.IntegerField(default=0, verbose_name="業務委託給与")

    common_expense_share = models.IntegerField(default=0, verbose_name="共通経費負担")
    reimbursement_carry_in = models.IntegerField(default=0, verbose_name="前月以前の未精算立替")
    reimbursement_current_month = models.IntegerField(default=0, verbose_name="当月承認立替")
    reimbursement_due = models.IntegerField(default=0, verbose_name="立替精算対象額")

    salary_due = models.IntegerField(default=0, verbose_name="給与確定額")
    salary_paid = models.IntegerField(default=0, verbose_name="給与支払済額")
    salary_unpaid = models.IntegerField(default=0, verbose_name="給与未払額")

    reimbursement_paid = models.IntegerField(default=0, verbose_name="立替精算支払済額")
    reimbursement_unpaid = models.IntegerField(default=0, verbose_name="立替未精算額")

    calculation_snapshot = models.JSONField(
        default=dict,
        blank=True,
        verbose_name="コーチ別計算スナップショット",
    )
    created_at = models.DateTimeField(default=timezone.now, verbose_name="作成日時")
    updated_at = models.DateTimeField(default=timezone.now, verbose_name="更新日時")

    class Meta:
        managed = False
        db_table = "club_coachmonthlysettlement"
        ordering = ["monthly_settlement_id", "coach_id"]
        verbose_name = "コーチ別月次精算"
        verbose_name_plural = "コーチ別月次精算"

    def __str__(self):
        return f"{self.monthly_settlement} / {self.coach}"

    def recalculate_balances(self):
        self.salary_unpaid = max(
            int(self.salary_due or 0) - int(self.salary_paid or 0),
            0,
        )
        self.reimbursement_unpaid = max(
            int(self.reimbursement_due or 0)
            - int(self.reimbursement_paid or 0),
            0,
        )
        return self.salary_unpaid, self.reimbursement_unpaid


class SettlementPayment(models.Model):
    PAYMENT_TYPE_SALARY = "salary"
    PAYMENT_TYPE_REIMBURSEMENT = "reimbursement"

    PAYMENT_TYPE_CHOICES = (
        (PAYMENT_TYPE_SALARY, "給与支払い"),
        (PAYMENT_TYPE_REIMBURSEMENT, "本人立替精算支払い"),
    )

    monthly_settlement = models.ForeignKey(
        MonthlySettlement,
        on_delete=models.PROTECT,
        related_name="payments",
        verbose_name="計上月次精算",
    )
    coach = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="settlement_payments",
        verbose_name="支払先コーチ",
    )
    payment_type = models.CharField(
        max_length=30,
        choices=PAYMENT_TYPE_CHOICES,
        verbose_name="支払種別",
    )
    amount = models.PositiveIntegerField(verbose_name="支払金額")
    paid_date = models.DateField(default=timezone.localdate, verbose_name="支払日")
    note = models.TextField(blank=True, default="", verbose_name="メモ")

    legacy_coach_expense_id = models.PositiveBigIntegerField(
        null=True,
        blank=True,
        verbose_name="既存支払記録ID",
    )

    is_reversed = models.BooleanField(default=False, verbose_name="取消済み")
    reversed_at = models.DateTimeField(null=True, blank=True, verbose_name="取消日時")
    reversed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reversed_settlement_payments",
        verbose_name="取消担当者",
    )
    reversal_note = models.TextField(blank=True, default="", verbose_name="取消理由")

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_settlement_payments",
        verbose_name="登録者",
    )
    created_at = models.DateTimeField(default=timezone.now, verbose_name="作成日時")
    updated_at = models.DateTimeField(default=timezone.now, verbose_name="更新日時")

    class Meta:
        managed = False
        db_table = "club_settlementpayment"
        ordering = ["paid_date", "id"]
        verbose_name = "精算支払記録"
        verbose_name_plural = "精算支払記録"

    def __str__(self):
        return (
            f"{self.paid_date:%Y-%m-%d} / "
            f"{self.coach} / {self.get_payment_type_display()} / "
            f"{self.amount:,}円"
        )

    def clean(self):
        if int(self.amount or 0) <= 0:
            raise ValidationError("支払金額は1円以上にしてください。")

    def _validate_wallet_payment(self):
        if not self._state.adding or self.is_reversed:
            return

        payment_amount = int(self.amount or 0)
        if payment_amount <= 0:
            raise ValidationError("支払金額は1円以上にしてください。")

        from .settlement_service import calculate_monthly_settlement

        result = calculate_monthly_settlement(
            self.monthly_settlement.year,
            self.monthly_settlement.month,
        )
        company_available = int(
            result.get("wallet_remaining_payable") or 0
        )
        target_row = next(
            (
                row
                for row in result.get("coach_rows", [])
                if getattr(row.get("coach"), "pk", None) == self.coach_id
            ),
            None,
        )
        coach_available = int((target_row or {}).get("unpaid_salary") or 0)

        if self.payment_type == self.PAYMENT_TYPE_REIMBURSEMENT:
            raise ValidationError(
                "会社＝財布方式では、立替返金は月末一括精算額に"
                "含まれます。支払種別は「給与支払い」を選択してください。"
            )
        if payment_amount > coach_available:
            raise ValidationError(
                "支払額がこのコーチの支払可能額を超えています。"
                f"支払可能上限は{coach_available:,}円です。"
            )
        if payment_amount > company_available:
            raise ValidationError(
                "支払額が会社の当月売上残高を超えています。"
                f"会社財布の支払可能残高は{company_available:,}円です。"
            )

    def save(self, *args, **kwargs):
        self._validate_wallet_payment()
        return super().save(*args, **kwargs)

    @property
    def active_amount(self):
        if self.is_reversed:
            return 0
        return int(self.amount or 0)

    @property
    def allocated_amount(self):
        if self.payment_type != self.PAYMENT_TYPE_REIMBURSEMENT:
            return 0
        result = self.expense_allocations.aggregate(total=Sum("amount"))
        return int(result.get("total") or 0)

    @property
    def unallocated_amount(self):
        if self.payment_type != self.PAYMENT_TYPE_REIMBURSEMENT:
            return 0
        return max(self.active_amount - self.allocated_amount, 0)

    def reverse(self, *, user, note=""):
        if self.is_reversed:
            return
        self.is_reversed = True
        self.reversed_at = timezone.now()
        self.reversed_by = user
        self.reversal_note = (note or "").strip()
        self.updated_at = timezone.now()
        self.save(
            update_fields=[
                "is_reversed",
                "reversed_at",
                "reversed_by",
                "reversal_note",
                "updated_at",
            ]
        )


class ExpenseSettlementAllocation(models.Model):
    payment = models.ForeignKey(
        SettlementPayment,
        on_delete=models.CASCADE,
        related_name="expense_allocations",
        verbose_name="立替精算支払",
    )
    expense = models.ForeignKey(
        "club.CoachExpense",
        on_delete=models.PROTECT,
        related_name="settlement_allocations",
        verbose_name="対象経費",
    )
    amount = models.PositiveIntegerField(verbose_name="充当金額")
    allocation_order = models.PositiveIntegerField(
        default=1,
        verbose_name="充当順",
    )
    created_at = models.DateTimeField(default=timezone.now, verbose_name="作成日時")

    class Meta:
        managed = False
        db_table = "club_expensesettlementallocation"
        ordering = ["payment_id", "allocation_order", "expense_id"]
        verbose_name = "経費精算充当"
        verbose_name_plural = "経費精算充当"

    def __str__(self):
        return f"{self.payment} → 経費ID {self.expense_id} / {self.amount:,}円"

    def clean(self):
        if int(self.amount or 0) <= 0:
            raise ValidationError("充当金額は1円以上にしてください。")
        if (
            self.payment_id
            and self.payment.payment_type
            != SettlementPayment.PAYMENT_TYPE_REIMBURSEMENT
        ):
            raise ValidationError("個人立替精算支払いだけが経費へ充当できます。")
