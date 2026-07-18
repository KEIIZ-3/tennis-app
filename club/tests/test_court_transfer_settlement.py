from types import SimpleNamespace

from django.test import SimpleTestCase

from club.settlement_service import (
    EXPENSE_APPROVAL_APPROVED,
    apply_court_transfer_expenses,
)


def coach_row():
    return {}


def expense_row(amount, *, payer_id, using_ids, approval_status="approved"):
    return {
        "expense": SimpleNamespace(amount=amount),
        "approval_status": approval_status,
        "meta": {
            "record_kind": "court_transfer",
            "approval_status": approval_status,
            "payer_coach_id": payer_id,
            "using_coach_ids": using_ids,
        },
    }


class CourtTransferSettlementTests(SimpleTestCase):
    def test_splits_usage_and_credits_payer(self):
        rows = {1: coach_row(), 2: coach_row(), 3: coach_row()}

        total = apply_court_transfer_expenses(
            rows,
            [
                expense_row(
                    1000,
                    payer_id=3,
                    using_ids=[1, 2, 2],
                    approval_status=EXPENSE_APPROVAL_APPROVED,
                )
            ],
        )

        self.assertEqual(total, 1000)
        self.assertEqual(rows[1]["court_use_deduction"], 500)
        self.assertEqual(rows[2]["court_use_deduction"], 500)
        self.assertEqual(rows[3]["court_advance_credit"], 1000)
        self.assertEqual(rows[1]["court_transfer_net"], -500)
        self.assertEqual(rows[3]["court_transfer_net"], 1000)

    def test_ignores_refunded_transfer(self):
        rows = {1: coach_row(), 2: coach_row()}

        total = apply_court_transfer_expenses(
            rows,
            [
                expense_row(
                    2400,
                    payer_id=2,
                    using_ids=[1],
                    approval_status="refunded",
                )
            ],
        )

        self.assertEqual(total, 0)
        self.assertEqual(rows[1]["court_use_deduction"], 0)
        self.assertEqual(rows[2]["court_advance_credit"], 0)
