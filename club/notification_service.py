"""Notification orchestration with frozen recipients and structured results."""

import logging
from dataclasses import dataclass

from django.db import transaction

from .models import LineAccountLink
from .notifications import send_email_to_address, send_line_to_id


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class NotificationRecipient:
    user_id: int | None
    email: str = ""
    line_user_id: str = ""


def freeze_recipients(users):
    """Resolve ORM objects now and deduplicate physical contact destinations."""
    recipients = []
    seen_users = set()
    for user in users:
        user_id = getattr(user, "pk", None)
        if user_id in seen_users:
            continue
        seen_users.add(user_id)
        email = (getattr(user, "email", "") or "").strip().lower()
        try:
            link = getattr(user, "line_link", None)
        except LineAccountLink.DoesNotExist:
            link = None
        line_user_id = ""
        if link and link.is_active:
            line_user_id = (link.line_user_id or "").strip()
        recipients.append(NotificationRecipient(user_id, email, line_user_id))
    return tuple(recipients)


def deliver(recipients, *, subject, message, media=("email",), email_fallback=False):
    """Send once per medium/address and return exact aggregate outcomes."""
    result = {"line_sent": 0, "email_sent": 0, "failed": 0, "skipped": 0}
    seen = set()
    for recipient in recipients:
        delivered = False
        attempted = False
        if "line" in media and recipient.line_user_id:
            key = ("line", recipient.line_user_id)
            if key in seen:
                attempted = True
                delivered = True
            else:
                seen.add(key)
                attempted = True
                if send_line_to_id(recipient.line_user_id, message):
                    result["line_sent"] += 1
                    delivered = True
        should_email = "email" in media and (not delivered or not email_fallback)
        if should_email and recipient.email:
            key = ("email", recipient.email)
            if key in seen:
                attempted = True
                delivered = True
            else:
                seen.add(key)
                attempted = True
                if send_email_to_address(recipient.email, subject, message):
                    result["email_sent"] += 1
                    delivered = True
        if not attempted:
            result["skipped"] += 1
        elif not delivered:
            result["failed"] += 1
    return result


def deliver_to_users(users, *, subject, message, media=("email",), email_fallback=False):
    return deliver(
        freeze_recipients(users),
        subject=subject,
        message=message,
        media=media,
        email_fallback=email_fallback,
    )


def schedule_delivery(recipients, *, subject, message, media=("email",), email_fallback=False):
    """Freeze all callback values; a rollback discards the callback."""
    frozen_recipients = tuple(recipients)
    frozen_subject = str(subject)
    frozen_message = str(message)
    frozen_media = tuple(media)

    def send_after_commit():
        try:
            return deliver(
                frozen_recipients,
                subject=frozen_subject,
                message=frozen_message,
                media=frozen_media,
                email_fallback=bool(email_fallback),
            )
        except Exception:
            logger.exception("notification delivery failed")
            return {"line_sent": 0, "email_sent": 0, "failed": len(frozen_recipients), "skipped": 0}

    transaction.on_commit(send_after_commit)
    return {"queued": len(frozen_recipients)}


def schedule_user_delivery(users, **kwargs):
    return schedule_delivery(freeze_recipients(users), **kwargs)
