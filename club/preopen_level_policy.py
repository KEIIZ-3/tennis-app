from contextvars import ContextVar
from datetime import date

from .models import (
    LessonWaitlist,
    Reservation,
    User,
    is_preopen_cash_lesson_date,
)

_preopen_level_free_request = ContextVar("preopen_level_free_request", default=False)
_PATCHED = False


def _looks_like_preopen_july_value(value) -> bool:
    if not value:
        return False
    text = str(value).strip()
    if not text:
        return False
    if text.startswith("2026-07") or text.startswith("2026/07") or text.startswith("2026/7"):
        return True
    return False


def _request_is_preopen_july(request) -> bool:
    if not request:
        return False

    values = []
    try:
        values.extend([
            request.GET.get("lesson_date"),
            request.POST.get("lesson_date"),
            request.GET.get("date"),
            request.POST.get("date"),
            request.GET.get("start"),
            request.POST.get("start"),
        ])
    except Exception:
        values = []

    for value in values:
        if _looks_like_preopen_july_value(value):
            return True

    try:
        year = request.GET.get("year") or request.POST.get("year")
        month = request.GET.get("month") or request.POST.get("month")
        if int(year or 0) == 2026 and int(month or 0) == 7:
            return True
    except Exception:
        pass

    return False


def _is_preopen_general_lesson_obj(obj) -> bool:
    if not obj:
        return False

    lesson_type = getattr(obj, "lesson_type", "")
    if lesson_type != Reservation.LESSON_GENERAL:
        return False

    start_at = getattr(obj, "start_at", None)
    if start_at:
        return is_preopen_cash_lesson_date(start_at)

    lesson_date = getattr(obj, "lesson_date", None)
    if lesson_date:
        if isinstance(lesson_date, date):
            return is_preopen_cash_lesson_date(lesson_date)
        return _looks_like_preopen_july_value(lesson_date)

    return False


def preopen_level_free_enabled() -> bool:
    return bool(_preopen_level_free_request.get())


class PreopenLevelFreeMiddleware:
    """
    2026年7月プレオープンの一般レッスンだけ、画面表示中のレベル制限を外すためのミドルウェア。

    views.py 側では日付情報なしで user.can_book_any_level() を呼ぶ箇所があるため、
    2026年7月のカレンダー・確認画面・予約POST中だけ判定を緩めます。
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        token = _preopen_level_free_request.set(_request_is_preopen_july(request))
        try:
            return self.get_response(request)
        finally:
            _preopen_level_free_request.reset(token)


def apply_preopen_level_policy():
    global _PATCHED
    if _PATCHED:
        return

    original_can_book_level = User.can_book_level
    original_can_book_any_level = User.can_book_any_level
    original_reservation_clean = Reservation.clean
    original_waitlist_clean = LessonWaitlist.clean

    def can_book_level_with_preopen(self, target_level: str) -> bool:
        if preopen_level_free_enabled():
            return True
        return original_can_book_level(self, target_level)

    def can_book_any_level_with_preopen(self, *target_levels: str) -> bool:
        if preopen_level_free_enabled():
            return True
        return original_can_book_any_level(self, *target_levels)

    def reservation_clean_with_preopen(self):
        token = None
        if _is_preopen_general_lesson_obj(self):
            token = _preopen_level_free_request.set(True)
        try:
            return original_reservation_clean(self)
        finally:
            if token is not None:
                _preopen_level_free_request.reset(token)

    def waitlist_clean_with_preopen(self):
        token = None
        if _is_preopen_general_lesson_obj(self):
            token = _preopen_level_free_request.set(True)
        try:
            return original_waitlist_clean(self)
        finally:
            if token is not None:
                _preopen_level_free_request.reset(token)

    User.can_book_level = can_book_level_with_preopen
    User.can_book_any_level = can_book_any_level_with_preopen
    Reservation.clean = reservation_clean_with_preopen
    LessonWaitlist.clean = waitlist_clean_with_preopen

    _PATCHED = True
