def ticket_revenue_from_consumptions(consumptions):
    """Return the participant's canonical ticket revenue for consumptions."""
    rows = list(consumptions)
    if not rows:
        return None
    return sum(
        int(consumption.unit_price_snapshot or 0)
        * int(consumption.tickets_used or 0)
        for consumption in rows
        if consumption.refunded_at is None
    )


def set_participant_ticket_price_snapshot(reservation, consumptions):
    """Set an unknown snapshot once, without repricing an existing reservation."""
    if reservation.participant_ticket_price_snapshot is not None:
        return reservation.participant_ticket_price_snapshot

    price = ticket_revenue_from_consumptions(consumptions)
    if price is None:
        return None

    reservation.__class__.objects.filter(
        pk=reservation.pk,
        participant_ticket_price_snapshot__isnull=True,
    ).update(participant_ticket_price_snapshot=price)
    reservation.participant_ticket_price_snapshot = price
    return price


def is_ball_expense_eligible(reservation):
    """Keep unknown legacy prices eligible; exclude known prices <= 1,000 yen."""
    price = getattr(reservation, "participant_ticket_price_snapshot", None)
    return price is None or int(price) > 1000
