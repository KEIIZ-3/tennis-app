def apply_today_lessons_count_patch():
    from . import views

    if getattr(views, "_today_lessons_count_patch_applied", False):
        return

    original_render = views.render

    def patched_render(request, template_name, context=None, *args, **kwargs):
        if template_name == "coach/today_lessons.html" and isinstance(context, dict):
            lesson_rows = context.get("lesson_rows") or []

            for row in lesson_rows:
                participant_rows = row.get("participant_rows") or []
                registered_member_rows = row.get("registered_member_rows") or []

                participant_count = len(participant_rows) + len(registered_member_rows)
                capacity = int(row.get("capacity") or 0)

                row["participant_count"] = participant_count
                row["remaining_count"] = max(capacity - participant_count, 0)
                row["is_full"] = participant_count >= capacity if capacity > 0 else False

            summary = context.get("summary")
            if isinstance(summary, dict):
                summary["participant_count"] = sum(
                    int(row.get("participant_count") or 0)
                    for row in lesson_rows
                )

                today_rows = context.get("today_rows") or []
                summary["today_participant_count"] = sum(
                    int(row.get("participant_count") or 0)
                    for row in today_rows
                )

        return original_render(request, template_name, context, *args, **kwargs)

    views.render = patched_render
    views._today_lessons_count_patch_applied = True
