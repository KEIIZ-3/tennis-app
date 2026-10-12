"""Reconcile legacy prepaid court details with canonical court transfers."""

from dataclasses import asdict, dataclass

from django.db import transaction
from django.utils import timezone

from .court_expense_transfer import (
    PAYER_KIND_COACH,
    PAYER_KIND_COMPANY_WALLET,
    _availability_for_update,
    save_court_transfer,
)
from .court_fee_service import calculate_availability_court_fee
from .court_transfer_service import get_current_court_transfer_for_availability
from .settlement_models import MonthlySettlement


@dataclass(frozen=True)
class PrepaidCourtTransferRepairResult:
    availability_id: int
    year: int
    month: int
    status: str
    reason: str
    amount: int | None = None
    payer_kind: str = ""
    expense_id: int | None = None

    def to_dict(self):
        return asdict(self)


def _local_start(availability):
    value = availability.start_at
    return timezone.localtime(value) if timezone.is_aware(value) else value


def inspect_prepaid_court_transfer(availability, *, lock=False):
    start = _local_start(availability)
    base = {
        "availability_id": availability.pk,
        "year": start.year,
        "month": start.month,
        "payer_kind": availability.court_payer_kind or "",
    }
    if get_current_court_transfer_for_availability(
        availability.pk, for_update=lock
    ) is not None:
        return PrepaidCourtTransferRepairResult(
            **base, status="skipped_existing", reason="court_transfer_exists"
        )
    if MonthlySettlement.objects.filter(
        year=start.year,
        month=start.month,
        status=MonthlySettlement.STATUS_CLOSED,
    ).exists():
        return PrepaidCourtTransferRepairResult(
            **base, status="skipped_closed_month", reason="accounting_month_closed"
        )
    if availability.court_payer_kind not in (
        PAYER_KIND_COMPANY_WALLET,
        PAYER_KIND_COACH,
    ):
        return PrepaidCourtTransferRepairResult(
            **base, status="skipped_missing_payer", reason="payer_not_set"
        )
    if (
        availability.court_payer_kind == PAYER_KIND_COACH
        and availability.court_payer_coach_id is None
    ):
        return PrepaidCourtTransferRepairResult(
            **base, status="skipped_missing_payer", reason="payer_coach_missing"
        )
    if availability.court_fee_overridden:
        amount = availability.court_fee_amount
    else:
        quote = calculate_availability_court_fee(availability)
        amount = None if quote is None else quote["total"]
    if amount is None or int(amount) < 0:
        return PrepaidCourtTransferRepairResult(
            **base, status="skipped_unresolved_amount", reason="court_fee_unresolved"
        )
    return PrepaidCourtTransferRepairResult(
        **base,
        status="candidate",
        reason="legacy_prepaid_court_transfer",
        amount=int(amount),
    )


@transaction.atomic
def repair_prepaid_court_transfer(availability_id):
    availability = (
        _availability_for_update(
            "coach", "substitute_coach", "court", "court_payer_coach"
        )
        .get(pk=availability_id)
    )
    preview = inspect_prepaid_court_transfer(availability, lock=True)
    if preview.status != "candidate":
        return preview
    actor = availability.substitute_coach or availability.coach
    expense, created = save_court_transfer(
        availability.pk,
        amount=preview.amount,
        payer_kind=availability.court_payer_kind,
        payer_coach=availability.court_payer_coach,
        actor=actor,
        court_cost_not_required=False,
        plain_note="Legacy prepaid court information repair",
    )
    return PrepaidCourtTransferRepairResult(
        availability_id=availability.pk,
        year=preview.year,
        month=preview.month,
        status="created" if created else "skipped_existing",
        reason="canonical_court_transfer_created" if created else "court_transfer_exists",
        amount=int(expense.amount),
        payer_kind=availability.court_payer_kind,
        expense_id=expense.pk,
    )
