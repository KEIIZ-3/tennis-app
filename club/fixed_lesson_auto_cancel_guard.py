from contextvars import ContextVar

from .models import FixedLesson, Reservation


AUTO_TRIM_REASON = "固定レッスンの開催回数変更による自動整理"
_SYNC_IN_PROGRESS = ContextVar("fixed_lesson_sync_in_progress", default=False)


def _reason_from_call(args, kwargs):
    if "reason" in kwargs:
        return kwargs.get("reason")

    # Reservation.cancel(self, created_by=None, reason="") を想定。
    # 将来シグネチャが変わっても、キーワード指定側は上の判定で安全に扱う。
    if len(args) >= 2:
        return args[1]
    return ""


def apply_fixed_lesson_auto_cancel_guard():
    """
    固定レッスン同期時の「開催回数変更による自動整理」だけを無効化する。

    会員本人キャンセル、雨天中止、固定メンバー解除など、その他の取消処理は
    元の Reservation.cancel() へそのまま委譲する。
    """
    if getattr(FixedLesson, "_auto_trim_guard_applied", False):
        return

    original_sync = FixedLesson.sync_future_reservations
    original_cancel = Reservation.cancel

    def guarded_sync(self, *args, **kwargs):
        token = _SYNC_IN_PROGRESS.set(True)
        try:
            return original_sync(self, *args, **kwargs)
        finally:
            _SYNC_IN_PROGRESS.reset(token)

    def guarded_cancel(self, *args, **kwargs):
        reason = str(_reason_from_call(args, kwargs) or "").strip()
        if _SYNC_IN_PROGRESS.get() and reason == AUTO_TRIM_REASON:
            # 開催回数設定の変更だけを理由に、既存の固定予約を自動取消ししない。
            return None
        return original_cancel(self, *args, **kwargs)

    FixedLesson.sync_future_reservations = guarded_sync
    Reservation.cancel = guarded_cancel
    FixedLesson._auto_trim_guard_applied = True


apply_fixed_lesson_auto_cancel_guard()
