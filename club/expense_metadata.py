import json


EXPENSE_NOTE_META_PREFIX = "__EXPENSE_META__"

EXPENSE_TYPE_PERSONAL = "personal"
EXPENSE_TYPE_COMMON = "common"
EXPENSE_TYPE_SALARY_PAYOUT = "salary_payout"
EXPENSE_TYPE_REIMBURSEMENT_PAYOUT = "reimbursement_payout"
EXPENSE_TYPE_COURT_TRANSFER = "court_transfer"

EXPENSE_APPROVAL_SUBMITTED = "submitted"
EXPENSE_APPROVAL_APPROVED = "approved"

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
