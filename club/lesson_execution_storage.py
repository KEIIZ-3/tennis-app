import json

from django.utils import timezone

from . import lesson_execution


NOTE_PREFIX = "__LESSON_EXECUTION__"
LEGACY_SNAPSHOT_KEY = "lesson_execution_statuses"


def _decode_note(settlement):
    text = str(getattr(settlement, "note", "") or "")
    if not text.startswith(NOTE_PREFIX):
        return {}, text

    try:
        first_line, plain_note = text.split("\n", 1)
    except ValueError:
        first_line = text
        plain_note = ""

    raw_json = first_line[len(NOTE_PREFIX):].strip()
    try:
        payload = json.loads(raw_json or "{}")
    except Exception:
        payload = {}

    status_map = payload.get("statuses") if isinstance(payload, dict) else {}
    if not isinstance(status_map, dict):
        status_map = {}
    return dict(status_map), plain_note


def read_status_map(settlement):
    status_map, _plain_note = _decode_note(settlement)
    if status_map:
        return status_map

    snapshot = dict(getattr(settlement, "calculation_snapshot", None) or {})
    legacy = snapshot.get(LEGACY_SNAPSHOT_KEY) or {}
    if isinstance(legacy, dict):
        return dict(legacy)
    return {}


def save_status(settlement, availability, status, user):
    status_map, plain_note = _decode_note(settlement)
    if not status_map:
        status_map = read_status_map(settlement)

    status_map[lesson_execution._availability_key(availability)] = {
        "status": status,
        "updated_at": timezone.now().isoformat(),
        "updated_by_id": getattr(user, "pk", None),
        "updated_by_name": lesson_execution._display_name(user),
    }

    payload = {
        "version": 1,
        "statuses": status_map,
    }
    settlement.note = (
        f"{NOTE_PREFIX}{json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}\n"
        f"{plain_note}"
    )
    settlement.updated_at = timezone.now()
    settlement.save(update_fields=["note", "updated_at"])


lesson_execution._read_status_map = read_status_map
lesson_execution._save_status = save_status
