import json

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from club.fixed_lesson_membership_service import rebind_occurrence_links
from club.models import FixedLesson, Reservation


class Command(BaseCommand):
    help = "Safely rebind one availability-backed occurrence (dry-run by default)"

    def add_arguments(self, parser):
        parser.add_argument("--reservation-id", type=int, required=True)
        parser.add_argument("--canonical-fixed-lesson-id", type=int, required=True)
        parser.add_argument("--apply", action="store_true")

    def handle(self, *args, **options):
        try:
            with transaction.atomic():
                reservation = Reservation.objects.select_for_update().select_related(
                    "availability", "fixed_lesson"
                ).get(pk=options["reservation_id"])
                fixed_lesson = FixedLesson.objects.select_for_update().get(
                    pk=options["canonical_fixed_lesson_id"]
                )
                if not reservation.availability_id:
                    raise ValidationError("Reservationにavailabilityがありません。")
                result = rebind_occurrence_links(
                    fixed_lesson,
                    reservation.availability,
                    reservation.start_at,
                    reservation.end_at,
                    apply=options["apply"],
                    preserve_parallel=False,
                    reservation_ids=[reservation.pk],
                )
                output = {
                    "applied": bool(options["apply"]),
                    "availability_id": reservation.availability_id,
                    "canonical_fixed_lesson_id": fixed_lesson.pk,
                    "before_fixed_lesson_id": reservation.fixed_lesson_id,
                    **result,
                }
                if not options["apply"]:
                    transaction.set_rollback(True)
        except (Reservation.DoesNotExist, FixedLesson.DoesNotExist, ValidationError) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(json.dumps(output, ensure_ascii=False, sort_keys=True))
