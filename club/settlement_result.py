from dataclasses import dataclass
from typing import Any, ClassVar, Mapping


@dataclass(init=False)
class MonthlySettlementResult(dict):
    """月次精算結果を表す、既存の辞書互換DTO。"""

    settlement: Any
    coach_rows: list
    is_closed: bool
    approved_common_expense_rows: list
    approved_personal_expense_rows: list
    submitted_personal_expense_rows: list
    preopen_paid_total: int
    preopen_unpaid_total: int
    ticket_amount_total: int
    ticket_purchase_total: int
    stringing_total: int
    cash_in_total: int
    approved_common_expense_total: int
    contractor_hourly_pay_total: int
    common_expense_base_total: int
    common_expense_participant_count: int
    salary_due_total: int
    reimbursement_due_total: int
    salary_paid_total: int
    reimbursement_paid_total: int
    unpaid_salary_total: int
    unpaid_reimbursement_total: int
    pending_personal_reimbursement_total: int
    cash_out_total: int
    company_balance: int
    opening_balance: int
    active_coach_count: int
    per_coach_common_expense: int
    payout_history_rows: list
    rain_refund_pending_rows: list
    rain_refund_pending_total: int
    rain_refunded_rows: list
    rain_refunded_total: int

    FIELD_NAMES: ClassVar[frozenset[str]] = frozenset(__annotations__)

    def __init__(self, **values):
        super().__init__(values)

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any]):
        if isinstance(values, cls):
            return values
        return cls(**dict(values))

    def __getattr__(self, name):
        if name in self.FIELD_NAMES:
            return self.get(name)
        raise AttributeError(name)

    def __setattr__(self, name, value):
        if name in self.FIELD_NAMES:
            self[name] = value
            return
        super().__setattr__(name, value)
