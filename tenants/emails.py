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

logger = logging.getLogger(__name__)


def email_delivery_configured():
    """Whether an email will actually reach an inbox, i.e. the prod SMTP
    backend is selected AND its host is set. Any other backend
    (console/locmem/dummy in dev & tests) is treated as "email works". A pure
    settings check with no tenant/guest specifics, it lives in the base
    `tenants` app so the guest portal, the campaign worker, and the
    contact-inquiry notifier can all gate on one definition without the base
    app having to import a downstream one (see DEPLOY.md "Mail")."""
    backend = getattr(settings, "EMAIL_BACKEND", "") or ""
    if backend.endswith("smtp.EmailBackend"):
        return bool(getattr(settings, "EMAIL_HOST", ""))
    return True


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
