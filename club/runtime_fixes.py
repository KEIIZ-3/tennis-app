from django.db import transaction
from django.db.models.signals import post_migrate
from django.dispatch import receiver
from django.utils import timezone

from .capacity_policy import general_lesson_capacity
from .models import (
    CoachAvailability,
    FixedLesson,
    Reservation,
    TicketConsumption,
    TicketPurchase,
    User,
    _ensure_ticket_purchase_stock_for_user,
    apply_ticket_change,
    ensure_accounting_month_is_open,
)


def coach_availability_effective_capacity(self):
    if self.lesson_type == self.LESSON_GENERAL:
        return general_lesson_capacity(self.coach_count, self.start_at)
    return int(self.capacity or 0)


def fixed_lesson_effective_capacity(self):
    if self.lesson_type == self.LESSON_GENERAL:
        return general_lesson_capacity(self.coach_count, self.start_date)
    return int(self.capacity or 0)


CoachAvailability.effective_capacity = coach_availability_effective_capacity
FixedLesson.effective_capacity = fixed_lesson_effective_capacity


def _sync_fixed_lesson_availabilities():
    """固定レッスンを正本として、生成済み開催枠の対象レベル・定員等を同期する。"""
    fixed_lessons = (
        FixedLesson.objects.filter(is_active=True)
        .select_related("coach", "coach_2", "coach_3", "court")
        .order_by("id")
    )

    for fixed_lesson in fixed_lessons:
        primary_coach = fixed_lesson.primary_coach()
        if not primary_coach:
            continue

        desired_coach_count = max(int(fixed_lesson.coach_count or 1), 1)
        desired_court_count = max(int(fixed_lesson.court_count or 1), 1)

        for target_date in fixed_lesson.scheduled_occurrence_dates():
            start_at, end_at = fixed_lesson._build_datetimes_for_date(target_date)
            availability_qs = CoachAvailability.objects.filter(
                coach=primary_coach,
                lesson_type=fixed_lesson.lesson_type,
                start_at=start_at,
                end_at=end_at,
            )
            if fixed_lesson.court_id:
                availability_qs = availability_qs.filter(court_id=fixed_lesson.court_id)

            availability = availability_qs.order_by("id").first()
            if not availability:
                continue

            if fixed_lesson.lesson_type == FixedLesson.LESSON_GENERAL:
                desired_capacity = general_lesson_capacity(desired_coach_count, target_date)
            else:
                desired_capacity = max(int(fixed_lesson.capacity or 0), 1)

            updated_fields = []
            if availability.target_level != fixed_lesson.target_level:
                availability.target_level = fixed_lesson.target_level
                updated_fields.append("target_level")
            if availability.target_level_2 != (fixed_lesson.target_level_2 or ""):
                availability.target_level_2 = fixed_lesson.target_level_2 or ""
                updated_fields.append("target_level_2")
            if availability.capacity != desired_capacity:
                availability.capacity = desired_capacity
                updated_fields.append("capacity")
            if availability.coach_count != desired_coach_count:
                availability.coach_count = desired_coach_count
                updated_fields.append("coach_count")
            if availability.court_count != desired_court_count:
                availability.court_count = desired_court_count
                updated_fields.append("court_count")

            if updated_fields:
                availability.save(update_fields=updated_fields)

            Reservation.objects.filter(
                fixed_lesson=fixed_lesson,
                start_at=start_at,
                end_at=end_at,
                status__in=[Reservation.STATUS_ACTIVE, Reservation.STATUS_PENDING],
            ).update(
                availability=availability,
                coach=primary_coach,
                court=availability.court,
                lesson_type=fixed_lesson.lesson_type,
                target_level=fixed_lesson.target_level,
                target_level_2=fixed_lesson.target_level_2 or "",
            )

        if fixed_lesson.lesson_type == FixedLesson.LESSON_GENERAL:
            desired_fixed_capacity = general_lesson_capacity(
                desired_coach_count,
                fixed_lesson.start_date,
            )
            if fixed_lesson.capacity != desired_fixed_capacity:
                FixedLesson.objects.filter(pk=fixed_lesson.pk).update(
                    capacity=desired_fixed_capacity,
                    coach_count=desired_coach_count,
                    court_count=desired_court_count,
                )


@receiver(post_migrate, dispatch_uid="club.sync_fixed_lesson_availabilities")
def sync_fixed_lesson_availabilities_after_migrate(sender, **kwargs):
    if getattr(sender, "label", "") != "club":
        return
    _sync_fixed_lesson_availabilities()


def consume_tickets_allowing_negative_balance(self, reason="reservation_use", created_by=None, note=""):
    """購入済み在庫を古い順で消費し、不足分は残高だけを-4枚まで許容する。"""
    ensure_accounting_month_is_open(self.start_at)
    if self.ticket_consumed_at or self.tickets_used <= 0:
        return None

    with transaction.atomic():
        _ensure_ticket_purchase_stock_for_user(self.user, created_by=created_by)

        locked_user = User.objects.select_for_update().get(pk=self.user.pk)
        purchases = list(
            TicketPurchase.objects.select_for_update()
            .filter(user=locked_user, remaining_tickets__gt=0)
            .order_by("purchased_at", "id")
        )

        remaining_to_consume = self.tickets_used
        for purchase in purchases:
            if remaining_to_consume <= 0:
                break

            use_count = min(int(purchase.remaining_tickets or 0), remaining_to_consume)
            if use_count <= 0:
                continue

            purchase.remaining_tickets -= use_count
            purchase.save(update_fields=["remaining_tickets"])

            TicketConsumption.objects.create(
                user=locked_user,
                purchase=purchase,
                reservation=self,
                fixed_lesson=self.fixed_lesson,
                tickets_used=use_count,
                unit_price_snapshot=purchase.unit_price,
            )
            remaining_to_consume -= use_count

        ledger = apply_ticket_change(
            user=locked_user,
            amount=-self.tickets_used,
            reason=reason,
            note=note or f"予約消費: {self.start_at:%Y-%m-%d %H:%M}",
            created_by=created_by,
            reservation=self,
            fixed_lesson=self.fixed_lesson,
        )

        consumed_at = timezone.now()
        Reservation.objects.filter(pk=self.pk).update(ticket_consumed_at=consumed_at)
        self.ticket_consumed_at = consumed_at
        self.user.ticket_balance = locked_user.ticket_balance
        return ledger


Reservation.consume_tickets = consume_tickets_allowing_negative_balance
