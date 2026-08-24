from .models import FamilyMember


def current_user_level_label(user):
    """Return the current account level label."""
    if not user:
        return ""
    try:
        return user.get_member_level_display()
    except Exception:
        return getattr(user, "member_level", "") or ""


def current_participant_level_label(
    parent,
    *,
    participant_type="self",
    family_member_id=None,
    snapshot_level_label="",
    is_guest=False,
):
    """Resolve current profile level while preserving reservation history.

    Accounts and family profiles are the source of truth for current-level
    displays. Guests and missing family profiles have no current profile, so
    their saved reservation snapshot remains the display fallback.
    """
    if is_guest:
        return snapshot_level_label or ""

    is_family = participant_type == "family" or bool(family_member_id)
    if not is_family:
        return current_user_level_label(parent)

    if family_member_id:
        member = FamilyMember.objects.filter(
            pk=family_member_id,
            parent=parent,
        ).first()
        if member:
            return member.get_member_level_display()
    return snapshot_level_label or ""
