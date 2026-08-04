import json

from django import forms
from .models import CoachExpense


EXPENSE_NOTE_META_PREFIX = "__EXPENSE_META__"
EXPENSE_TYPE_COMMON = "common"
EXPENSE_TYPE_PERSONAL = "personal"
EXPENSE_TYPE_COURT_TRANSFER = "court_transfer"

EXPENSE_TYPE_CHOICES = (
    (EXPENSE_TYPE_COMMON, "共通経費（給与計算に含める）"),
    (EXPENSE_TYPE_PERSONAL, "個人経費（給与計算に含めない）"),
)


def _parse_note(stored_note):
    text = str(stored_note or "")
    defaults = {
        "expense_type": EXPENSE_TYPE_COMMON,
        "plain_note": text.strip(),
    }
    if not text.startswith(EXPENSE_NOTE_META_PREFIX):
        return defaults

    try:
        first_line, plain_note = text.split("\n", 1)
    except ValueError:
        first_line = text
        plain_note = ""

    raw_json = first_line[len(EXPENSE_NOTE_META_PREFIX):].strip()
    try:
        metadata = json.loads(raw_json or "{}")
    except Exception:
        metadata = {}

    return {
        **metadata,
        "expense_type": metadata.get("expense_type") or EXPENSE_TYPE_COMMON,
        "plain_note": (plain_note or "").strip(),
    }


def _serialize_note(metadata, plain_note):
    stored_metadata = {
        key: value
        for key, value in dict(metadata or {}).items()
        if key != "plain_note"
    }
    first_line = (
        f"{EXPENSE_NOTE_META_PREFIX}"
        f"{json.dumps(stored_metadata, ensure_ascii=False, separators=(',', ':'))}"
    )
    plain_text = str(plain_note or "").strip()
    return f"{first_line}\n{plain_text}" if plain_text else first_line


class EditableExpenseTypeAdminForm(forms.ModelForm):
    expense_type = forms.ChoiceField(
        label="経費区分",
        choices=EXPENSE_TYPE_CHOICES,
        help_text=(
            "共通経費は給与計算へ反映されます。"
            "個人経費は給与計算・月次精算の共通経費履歴に含まれません。"
        ),
    )

    class Meta:
        model = CoachExpense
        fields = "__all__"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._stored_metadata = {}

        if "created_by" in self.fields:
            from .admin import coach_user_queryset

            self.fields["created_by"].queryset = coach_user_queryset()
            self.fields["created_by"].required = False

        parsed = _parse_note(getattr(self.instance, "note", ""))
        self._stored_metadata = {
            key: value
            for key, value in parsed.items()
            if key != "plain_note"
        }

        current_type = parsed.get("expense_type") or EXPENSE_TYPE_COMMON
        self.initial["note"] = parsed.get("plain_note", "")

        if current_type == EXPENSE_TYPE_COURT_TRANSFER:
            self.fields["expense_type"].choices = (
                (EXPENSE_TYPE_COURT_TRANSFER, "コート代登録（変更不可）"),
            )
            self.fields["expense_type"].disabled = True
            self.fields["expense_type"].help_text = (
                "コート代登録は専用処理で使用するため、経費区分を変更できません。"
            )
        else:
            self.initial["expense_type"] = (
                current_type
                if current_type in {EXPENSE_TYPE_COMMON, EXPENSE_TYPE_PERSONAL}
                else EXPENSE_TYPE_COMMON
            )

    def clean_expense_type(self):
        current_type = self._stored_metadata.get("expense_type")
        if current_type == EXPENSE_TYPE_COURT_TRANSFER:
            return EXPENSE_TYPE_COURT_TRANSFER
        return self.cleaned_data["expense_type"]

    def save(self, commit=True):
        instance = super().save(commit=False)
        metadata = dict(self._stored_metadata)
        metadata["expense_type"] = self.cleaned_data["expense_type"]
        instance.note = _serialize_note(metadata, self.cleaned_data.get("note"))
        if commit:
            instance.save()
            self.save_m2m()
        return instance


class ExpenseTypeAdminMixin:
    form = EditableExpenseTypeAdminForm

    def expense_type_admin(self, obj):
        expense_type = _parse_note(getattr(obj, "note", "")).get("expense_type")
        labels = {
            EXPENSE_TYPE_COMMON: "共通経費",
            EXPENSE_TYPE_PERSONAL: "個人経費",
            EXPENSE_TYPE_COURT_TRANSFER: "コート代登録",
        }
        return labels.get(expense_type, "共通経費")

    expense_type_admin.short_description = "経費区分"
