def standard_ticket_count(*, lesson_type, duration_hours, participant_count=1,
                          custom_ticket_count=0):
    """Return the canonical ticket requirement for one participant."""
    hours = max(int(duration_hours or 0), 1)
    participants = max(int(participant_count or 0), 1)
    if lesson_type == "private":
        return hours * 2
    if lesson_type == "group":
        return hours * participants
    if lesson_type == "event":
        return int(custom_ticket_count or 0)
    return 1
