import json


EXPENSE_NOTE_META_PREFIX = "__EXPENSE_META__"

EXPENSE_TYPE_PERSONAL = "personal"
EXPENSE_TYPE_COMMON = "common"
EXPENSE_TYPE_SALARY_PAYOUT = "salary_payout"
EXPENSE_TYPE_REIMBURSEMENT_PAYOUT = "reimbursement_payout"
EXPENSE_TYPE_COURT_TRANSFER = "court_transfer"

EXPENSE_APPROVAL_SUBMITTED = "submitted"
EXPENSE_APPROVAL_APPROVED = "approved"

EXPENSE_TYPE_LABELS = {
    EXPENSE_TYPE_PERSONAL: "個人経費（給与計算対象外）",
    EXPENSE_TYPE_COMMON: "共通経費",
    EXPENSE_TYPE_SALARY_PAYOUT: "給与支払い",
    EXPENSE_TYPE_REIMBURSEMENT_PAYOUT: "本人立替精算支払い",
    EXPENSE_TYPE_COURT_TRANSFER: "コート代振替",
}

DEFAULT_EXPENSE_META = {
    "expense_type": EXPENSE_TYPE_COMMON,
    "receipt_status": "none",
    "receipt_check_status": "unchecked",
    "approval_status": EXPENSE_APPROVAL_APPROVED,
}



def parse_expense_note(stored_note):
    text = str(stored_note or "")
    if not text.startswith(EXPENSE_NOTE_META_PREFIX):
        return {
            **DEFAULT_EXPENSE_META,
            "plain_note": text.strip(),
        }

    try:
        first_line, plain_note = text.split("\n", 1)
    except ValueError:
        first_line = text
        plain_note = ""

    raw_meta = first_line[len(EXPENSE_NOTE_META_PREFIX):].strip()
    try:
        parsed = json.loads(raw_meta or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        parsed = {}

    if not isinstance(parsed, dict):
        parsed = {}

    return {
        **DEFAULT_EXPENSE_META,
        **parsed,
        "plain_note": str(plain_note or "").strip(),
    }



def build_expense_note(meta=None, plain_note=""):
    normalized_meta = {
        **DEFAULT_EXPENSE_META,
        **(meta if isinstance(meta, dict) else {}),
    }
    metadata_json = json.dumps(
        normalized_meta,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return (
        f"{EXPENSE_NOTE_META_PREFIX}{metadata_json}\n"
        f"{str(plain_note or '').strip()}"
    )



def expense_plain_note(stored_note):
    return parse_expense_note(stored_note).get("plain_note", "")



def expense_meta_row(expense):
    meta = parse_expense_note(getattr(expense, "note", ""))
    expense_type = str(meta.get("expense_type") or EXPENSE_TYPE_COMMON)
    approval_status = str(
        meta.get("approval_status") or EXPENSE_APPROVAL_APPROVED
    )
    is_payout = (
        str(meta.get("record_kind") or "") == "coach_payout"
        or expense_type
        in {
            EXPENSE_TYPE_SALARY_PAYOUT,
            EXPENSE_TYPE_REIMBURSEMENT_PAYOUT,
        }
    )

    return {
        "expense": expense,
        "meta": meta,
        "plain_note": meta.get("plain_note", ""),
        "expense_type": expense_type,
        "expense_type_label": EXPENSE_TYPE_LABELS.get(
            expense_type,
            expense_type,
        ),
        "approval_status": approval_status,
        "is_payout": is_payout,
    }
