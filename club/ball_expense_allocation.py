from collections import defaultdict


def held_participant_count_by_coach(
    reservations,
    status_map,
    *,
    eligible_coach_ids,
    execution_slot_key,
    reservation_coaches,
):
    """Count canonical Reservation participants for held lesson occurrences."""
    eligible_ids = set(eligible_coach_ids or [])
    participant_ids_by_slot = defaultdict(set)
    coach_ids_by_slot = defaultdict(set)

    for reservation in reservations:
        slot_key = execution_slot_key(reservation)
        if not slot_key:
            continue
        if (status_map.get(slot_key) or {}).get("status") != "held":
            continue

        # A Reservation represents exactly one actual participant.  The user is
        # only the contact account and may own multiple family reservations.
        reservation_id = getattr(reservation, "pk", None)
        if reservation_id is None:
            reservation_id = id(reservation)
        participant_ids_by_slot[slot_key].add(reservation_id)
        coach_ids_by_slot[slot_key].update(
            coach.pk
            for coach in reservation_coaches(reservation)
            if getattr(coach, "pk", None) in eligible_ids
        )

    counts = defaultdict(int)
    for slot_key, participant_ids in participant_ids_by_slot.items():
        for coach_id in coach_ids_by_slot[slot_key]:
            counts[coach_id] += len(participant_ids)
    return dict(counts)


def split_amount_by_participant_count(
    amount,
    coach_ids,
    participant_count_by_coach,
    *,
    money,
    split_evenly,
):
    """Allocate an amount by participants while preserving the legacy rounding."""
    unique_ids = list(dict.fromkeys(coach_id for coach_id in coach_ids if coach_id))
    weights = {
        coach_id: max(money((participant_count_by_coach or {}).get(coach_id)), 0)
        for coach_id in unique_ids
    }
    total_weight = sum(weights.values())
    if total_weight <= 0:
        return split_evenly(amount, unique_ids)

    total_amount = money(amount)
    allocations = {}
    for coach_id in unique_ids:
        numerator = total_amount * weights[coach_id]
        allocations[coach_id] = (numerator * 2 + total_weight) // (
            total_weight * 2
        )

    difference = total_amount - sum(allocations.values())
    adjustment_order = sorted(
        unique_ids,
        key=lambda coach_id: (weights[coach_id], unique_ids.index(coach_id)),
    )
    step = 1 if difference > 0 else -1
    for index in range(abs(difference)):
        coach_id = adjustment_order[index % len(adjustment_order)]
        allocations[coach_id] += step
    return allocations
