"""Read-only diagnostics for saved settlement, payment, and carry integrity."""

from collections import defaultdict

from .settlement_models import (
    CoachMonthlySettlement,
    MonthlySettlement,
    SettlementPayment,
)


def _money(value):
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _month_key(year, month):
    return int(year) * 12 + int(month) - 1


def _month_label(key):
    return {"year": key // 12, "month": key % 12 + 1}


def _previous_key(year, month):
    return _month_key(year, month) - 1


def _carry_values(row):
    snapshot = dict(row.get("calculation_snapshot") or {})
    return {
        "negative_carry_in": max(_money(snapshot.get("negative_carry_in")), 0),
        "unpaid_salary_carry_in": max(
            _money(snapshot.get("unpaid_salary_carry_in")), 0
        ),
    }


def _source_carry_values(row):
    snapshot = dict(row.get("calculation_snapshot") or {})
    return {
        "negative_carry_in": max(_money(snapshot.get("negative_carry")), 0),
        "unpaid_salary_carry_in": max(_money(row.get("salary_unpaid")), 0),
    }


def diagnose_settlement_carry_integrity():
    """Inspect existing saved values without invoking any persistence path."""
    settlements = list(
        MonthlySettlement.objects.order_by("year", "month", "id").values(
            "id", "year", "month", "status", "opening_balance",
            "closing_balance", "salary_cash_out", "reimbursement_cash_out",
            "unpaid_salary_total", "unpaid_reimbursement_total", "closed_at",
            "reopened_at", "updated_at",
        )
    )
    coach_rows = list(
        CoachMonthlySettlement.objects.order_by(
            "monthly_settlement__year", "monthly_settlement__month",
            "monthly_settlement_id", "coach_id", "id",
        ).values(
            "id", "monthly_settlement_id", "coach_id", "salary_unpaid",
            "reimbursement_unpaid", "calculation_snapshot",
        )
    )
    payments = list(
        SettlementPayment.objects.order_by(
            "monthly_settlement__year", "monthly_settlement__month",
            "monthly_settlement_id", "coach_id", "payment_type", "id",
        ).values(
            "id", "monthly_settlement_id", "coach_id", "payment_type",
            "amount", "paid_date", "note", "legacy_coach_expense_id",
            "is_reversed",
        )
    )

    settlement_by_id = {row["id"]: row for row in settlements}
    settlement_by_month = {
        _month_key(row["year"], row["month"]): row for row in settlements
    }
    coach_rows_by_settlement = defaultdict(list)
    coach_rows_by_coach = defaultdict(list)
    for row in coach_rows:
        settlement = settlement_by_id[row["monthly_settlement_id"]]
        enriched = dict(row, year=settlement["year"], month=settlement["month"])
        coach_rows_by_settlement[row["monthly_settlement_id"]].append(enriched)
        coach_rows_by_coach[row["coach_id"]].append(enriched)

    active_payment_totals = defaultdict(int)
    duplicate_groups = defaultdict(list)
    legacy_groups = defaultdict(list)
    payment_summary = {
        "total": len(payments), "active": 0, "reversed": 0,
        "salary": 0, "reimbursement": 0,
        "reimbursement_active": 0, "reimbursement_reversed": 0,
        "reimbursement_with_legacy_link": 0,
        "reimbursement_without_legacy_link": 0,
    }
    reimbursement_by_coach_month = defaultdict(lambda: {"amount": 0, "ids": []})
    for payment in payments:
        payment_summary[payment["payment_type"]] += 1
        payment_summary["reversed" if payment["is_reversed"] else "active"] += 1
        if not payment["is_reversed"]:
            active_payment_totals[(payment["monthly_settlement_id"], payment["coach_id"], payment["payment_type"])] += _money(payment["amount"])
            duplicate_groups[(
                payment["monthly_settlement_id"], payment["coach_id"],
                payment["payment_type"], _money(payment["amount"]),
                payment["paid_date"], payment["note"],
            )].append(payment["id"])
        if payment["legacy_coach_expense_id"] is not None:
            legacy_groups[payment["legacy_coach_expense_id"]].append(payment)
        if payment["payment_type"] == SettlementPayment.PAYMENT_TYPE_REIMBURSEMENT:
            payment_summary[
                "reimbursement_reversed" if payment["is_reversed"]
                else "reimbursement_active"
            ] += 1
            payment_summary[
                "reimbursement_with_legacy_link"
                if payment["legacy_coach_expense_id"] is not None
                else "reimbursement_without_legacy_link"
            ] += 1
            if not payment["is_reversed"]:
                key = (payment["monthly_settlement_id"], payment["coach_id"])
                reimbursement_by_coach_month[key]["amount"] += _money(payment["amount"])
                reimbursement_by_coach_month[key]["ids"].append(payment["id"])

    duplicate_payments = []
    for key, ids in duplicate_groups.items():
        if len(ids) < 2:
            continue
        settlement_id, coach_id, payment_type, amount, paid_date, _note = key
        settlement = settlement_by_id[settlement_id]
        duplicate_payments.append({
            "monthly_settlement_id": settlement_id, "year": settlement["year"],
            "month": settlement["month"], "coach_id": coach_id,
            "payment_type": payment_type, "amount": amount,
            "paid_date": paid_date.isoformat(), "active_payment_ids": sorted(ids),
            "count": len(ids),
        })

    duplicate_legacy_links = []
    for legacy_id, rows in legacy_groups.items():
        if len(rows) > 1:
            duplicate_legacy_links.append({
                "legacy_coach_expense_id": legacy_id,
                "payment_ids": sorted(row["id"] for row in rows),
                "count": len(rows),
            })

    opening_balance_checks = []
    opening_findings = 0
    for settlement in settlements:
        previous = settlement_by_month.get(
            _previous_key(settlement["year"], settlement["month"])
        )
        matches = (
            None if previous is None else
            _money(settlement["opening_balance"]) == _money(previous["closing_balance"])
        )
        is_finding = bool(
            previous and previous["status"] == MonthlySettlement.STATUS_CLOSED
            and settlement["status"] == MonthlySettlement.STATUS_CLOSED
            and not matches
        )
        opening_findings += int(is_finding)
        opening_balance_checks.append({
            "settlement_id": settlement["id"], "year": settlement["year"],
            "month": settlement["month"], "status": settlement["status"],
            "opening_balance": _money(settlement["opening_balance"]),
            "previous_settlement_id": previous["id"] if previous else None,
            "previous_status": previous["status"] if previous else None,
            "previous_closing_balance": _money(previous["closing_balance"]) if previous else None,
            "matches_previous_closing": matches, "is_finding": is_finding,
        })

    missing_month_carry_cases = []
    missing_carry_findings = 0
    for coach_id, rows in sorted(coach_rows_by_coach.items()):
        for source, target in zip(rows, rows[1:]):
            source_key = _month_key(source["year"], source["month"])
            target_key = _month_key(target["year"], target["month"])
            if target_key - source_key <= 1:
                continue
            source_settlement = settlement_by_id[source["monthly_settlement_id"]]
            if source_settlement["status"] != MonthlySettlement.STATUS_CLOSED:
                continue
            target_carry = _carry_values(target)
            is_finding = any(target_carry.values())
            missing_carry_findings += int(is_finding)
            missing_month_carry_cases.append({
                "coach_id": coach_id,
                "source_closed_month": _month_label(source_key),
                "missing_months": [_month_label(key) for key in range(source_key + 1, target_key)],
                "target_month": _month_label(target_key),
                "target_carry_value": target_carry, "is_finding": is_finding,
            })

    legacy_reimbursement_impacts = []
    for (settlement_id, coach_id), reimbursement in sorted(reimbursement_by_coach_month.items()):
        saved_row = next(
            (row for row in coach_rows_by_settlement[settlement_id] if row["coach_id"] == coach_id),
            None,
        )
        if saved_row is None:
            continue
        salary_paid = active_payment_totals[(settlement_id, coach_id, SettlementPayment.PAYMENT_TYPE_SALARY)]
        reimbursement_paid = reimbursement["amount"]
        entitlement = _money(
            (saved_row.get("calculation_snapshot") or {}).get(
                "wallet_final_entitlement", 0
            )
        )
        hypothetical = max(entitlement - salary_paid, 0)
        current_virtual = max(entitlement - salary_paid - reimbursement_paid, 0)
        legacy_reimbursement_impacts.append({
            "settlement_id": settlement_id, "coach_id": coach_id,
            "reimbursement_paid": reimbursement_paid, "salary_paid": salary_paid,
            "salary_unpaid_saved": _money(saved_row["salary_unpaid"]),
            "hypothetical_salary_unpaid_without_reimbursement_payment": hypothetical,
            "difference": hypothetical - current_virtual,
            "salary_payment_coexists": salary_paid > 0,
            "active_reimbursement_payment_ids": sorted(reimbursement["ids"]),
        })

    double_carry_findings = []
    for row in coach_rows:
        negative_carry = max(
            _money((row.get("calculation_snapshot") or {}).get("negative_carry")), 0
        )
        if _money(row["salary_unpaid"]) > 0 and negative_carry > 0:
            double_carry_findings.append({
                "settlement_id": row["monthly_settlement_id"],
                "coach_id": row["coach_id"], "salary_unpaid": _money(row["salary_unpaid"]),
                "negative_carry": negative_carry,
                "snapshot_key": "negative_carry",
            })

    total_mismatches = []
    for settlement in settlements:
        rows = coach_rows_by_settlement[settlement["id"]]
        comparisons = {
            "unpaid_salary_total": (
                _money(settlement["unpaid_salary_total"]),
                sum(_money(row["salary_unpaid"]) for row in rows),
            ),
            "unpaid_reimbursement_total": (
                _money(settlement["unpaid_reimbursement_total"]),
                sum(_money(row["reimbursement_unpaid"]) for row in rows),
            ),
            "salary_cash_out": (
                _money(settlement["salary_cash_out"]),
                sum(value for (sid, _cid, kind), value in active_payment_totals.items()
                    if sid == settlement["id"] and kind == SettlementPayment.PAYMENT_TYPE_SALARY),
            ),
            "reimbursement_cash_out": (
                _money(settlement["reimbursement_cash_out"]),
                sum(value for (sid, _cid, kind), value in active_payment_totals.items()
                    if sid == settlement["id"] and kind == SettlementPayment.PAYMENT_TYPE_REIMBURSEMENT),
            ),
        }
        for field, (saved, expected) in comparisons.items():
            if saved != expected:
                total_mismatches.append({
                    "settlement_id": settlement["id"], "year": settlement["year"],
                    "month": settlement["month"], "field": field,
                    "saved_value": saved, "expected_value": expected,
                    "difference": saved - expected,
                })

    reopen_chain_checks = []
    reopen_findings = 0
    for source in settlements:
        if source["reopened_at"] is None:
            continue
        next_settlement = settlement_by_month.get(
            _month_key(source["year"], source["month"]) + 1
        )
        mismatches = []
        if next_settlement:
            source_rows = {row["coach_id"]: row for row in coach_rows_by_settlement[source["id"]]}
            next_rows = {row["coach_id"]: row for row in coach_rows_by_settlement[next_settlement["id"]]}
            for coach_id in sorted(set(source_rows) | set(next_rows)):
                expected = (
                    _source_carry_values(source_rows.get(coach_id, {}))
                    if source["status"] == MonthlySettlement.STATUS_CLOSED else
                    {"negative_carry_in": 0, "unpaid_salary_carry_in": 0}
                )
                actual = _carry_values(next_rows.get(coach_id, {}))
                if actual != expected:
                    mismatches.append({"coach_id": coach_id, "expected": expected, "actual": actual})
        is_finding = bool(next_settlement and mismatches)
        reopen_findings += int(is_finding)
        reopen_chain_checks.append({
            "source_settlement_id": source["id"],
            "source_month": _month_label(_month_key(source["year"], source["month"])),
            "reopened_at": source["reopened_at"].isoformat(),
            "source_status": source["status"],
            "next_settlement_id": next_settlement["id"] if next_settlement else None,
            "next_month": _month_label(_month_key(next_settlement["year"], next_settlement["month"])) if next_settlement else None,
            "next_updated_at": next_settlement["updated_at"].isoformat() if next_settlement else None,
            "possible_stale_carry": is_finding, "judgement": "mismatch" if is_finding else "indeterminate",
            "carry_mismatches": mismatches,
        })

    reimbursement_monthly_summary = []
    for (settlement_id, coach_id), values in sorted(reimbursement_by_coach_month.items()):
        settlement = settlement_by_id[settlement_id]
        reimbursement_monthly_summary.append({
            "settlement_id": settlement_id, "year": settlement["year"],
            "month": settlement["month"], "coach_id": coach_id,
            "amount": values["amount"], "active_payment_ids": sorted(values["ids"]),
        })

    finding_count = (
        len(duplicate_payments) + len(duplicate_legacy_links) + opening_findings
        + missing_carry_findings + len(double_carry_findings)
        + len(total_mismatches) + reopen_findings
    )
    return {
        "settlement_count": len(settlements), "payment_summary": payment_summary,
        "reimbursement_by_coach_month": reimbursement_monthly_summary,
        "duplicate_payments": duplicate_payments,
        "duplicate_legacy_links": duplicate_legacy_links,
        "opening_balance_checks": opening_balance_checks,
        "missing_month_carry_cases": missing_month_carry_cases,
        "legacy_reimbursement_impacts": legacy_reimbursement_impacts,
        "reopen_chain_checks": reopen_chain_checks,
        "double_carry_findings": double_carry_findings,
        "total_mismatches": total_mismatches, "finding_count": finding_count,
    }
