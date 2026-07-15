"""Courtesy email notification for a new landing-page contact inquiry.

The ContactInquiry row is the source of truth (visible in /admin); this email
is purely a heads-up so a new lead doesn't sit unnoticed. It therefore must
NEVER make the submission fail: it's skipped while email delivery is
unconfigured (the same email_delivery_configured() gate the guest portal and
campaign worker use -- see DEPLOY.md "Mail"), and a transport error is logged
and swallowed. The moment prod SMTP is wired up, notifications start flowing
with no code change.
"""

import logging

from django.conf import settings
from django.core.mail import EmailMessage

from guests.services import email_delivery_configured

logger = logging.getLogger(__name__)


def notify_contact_inquiry(inquiry):
    """Best-effort: email settings.PLATFORM_CONTACT_EMAIL about `inquiry`.
    Returns True if a notification was actually sent."""
    if not email_delivery_configured():
        return False

    body_lines = [
        f"Name:  {inquiry.name}",
        f"Email: {inquiry.email}",
    ]
    if inquiry.venue:
        body_lines.append(f"Venue: {inquiry.venue}")
    body_lines += ["", inquiry.message, "", "Review and mark handled in /admin."]

    email = EmailMessage(
        subject=f"New contact inquiry from {inquiry.name}",
        body="\n".join(body_lines),
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=[settings.PLATFORM_CONTACT_EMAIL],
        # Reply lands with the prospective venue, not in the no-reply void.
        reply_to=[inquiry.email],
    )
    try:
        email.send(fail_silently=False)
    except Exception:
        logger.exception(
            "Contact-inquiry notification email failed; inquiry %s is saved "
            "and visible in /admin regardless.",
            inquiry.pk,
        )
        return False
    return True
