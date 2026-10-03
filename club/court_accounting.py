from django.core.exceptions import ValidationError


PAYER_COACH = "coach"
PAYER_COMPANY_WALLET = "company_wallet"
PAYER_KIND_CHOICES = (
    ("", "未設定"),
    (PAYER_COMPANY_WALLET, "会社の財布"),
    (PAYER_COACH, "コーチ"),
)

ACCOUNT_COACH = "coach"
ACCOUNT_OTHER = "other"
ACCOUNT_KIND_CHOICES = (
    ("", "未設定"),
    (ACCOUNT_COACH, "コーチ"),
    (ACCOUNT_OTHER, "その他"),
)


def validate_court_payer(kind, coach_id, *, field_prefix="court_payer"):
    if kind == PAYER_COACH and not coach_id:
        raise ValidationError({f"{field_prefix}_coach": "コーチ支払元を選択してください。"})
    if kind == PAYER_COMPANY_WALLET and coach_id:
        raise ValidationError({f"{field_prefix}_coach": "会社の財布ではコーチを指定できません。"})
    if not kind and coach_id:
        raise ValidationError({f"{field_prefix}_coach": "支払元種別を選択してください。"})


def validate_booking_account(kind, coach_id, other, *, field_prefix="court_booking_account"):
    other = (other or "").strip()
    if kind == ACCOUNT_COACH:
        if not coach_id:
            raise ValidationError({f"{field_prefix}_coach": "予約アカウントのコーチを選択してください。"})
        if other:
            raise ValidationError({f"{field_prefix}_other": "コーチ選択時はその他欄を空にしてください。"})
    elif kind == ACCOUNT_OTHER:
        if coach_id:
            raise ValidationError({f"{field_prefix}_coach": "その他選択時はコーチを指定できません。"})
        if not other:
            raise ValidationError({f"{field_prefix}_other": "その他の予約アカウント名を入力してください。"})
    elif coach_id or other:
        raise ValidationError({field_prefix + "_kind": "予約アカウント種別を選択してください。"})


def payer_display(kind, coach):
    if kind == PAYER_COMPANY_WALLET:
        return "会社の財布"
    if kind == PAYER_COACH and coach:
        return coach.display_name()
    return "未設定"


def booking_account_display(kind, coach, other):
    if kind == ACCOUNT_COACH and coach:
        return coach.display_name()
    if kind == ACCOUNT_OTHER:
        return (other or "").strip() or "未設定"
    return "未設定"
