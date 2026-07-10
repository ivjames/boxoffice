"""Guest (ticket-buyer) session + magic-link plumbing.

A guest is "signed in" purely by a value in the Django session -- there is
no auth backend and no request.user involvement (request.user is reserved
for staff, see accounts/). Everything about who the current guest is funnels
through get_current_guest() so the org-scoping check (a session started on
one tenant must never resolve a guest on another) lives in exactly one
place, mirroring how accounts.permissions.get_membership is the one gate for
staff.
"""

import logging

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.urls import reverse

from .models import GuestAccount
from .tokens import make_login_token

logger = logging.getLogger(__name__)

# Session keys. Both are stored so get_current_guest can reject a guest id
# that belongs to a different tenant than the one this request resolved to.
_SESSION_GUEST_ID = "guest_account_id"
_SESSION_GUEST_ORG = "guest_org_id"


def login_guest(request, guest):
    """Mark `guest` as the signed-in guest for this session. Called after a
    magic link is verified, and also right after a purchase completes on the
    buyer's own request (checkout stub/test success, or the Stripe
    success-page landing) so a first-time buyer is already signed in to see
    all their tickets without a round-trip through email."""
    request.session[_SESSION_GUEST_ID] = guest.pk
    request.session[_SESSION_GUEST_ORG] = guest.organization_id


def logout_guest(request):
    request.session.pop(_SESSION_GUEST_ID, None)
    request.session.pop(_SESSION_GUEST_ORG, None)


def get_current_guest(request):
    """The GuestAccount signed in on this request, or None.

    Returns None unless the session carries a guest id AND that guest belongs
    to request.organization -- so a cookie that somehow rode along to another
    subdomain can't expose one tenant's guest to another (the same defense
    accounts.permissions applies to staff Memberships). Cached on the request
    so the context processor and a view can both call it without a second
    query."""
    organization = getattr(request, "organization", None)
    if organization is None:
        return None
    if hasattr(request, "_guest_cache"):
        return request._guest_cache

    guest = None
    guest_id = request.session.get(_SESSION_GUEST_ID)
    guest_org = request.session.get(_SESSION_GUEST_ORG)
    if guest_id and str(guest_org) == str(organization.pk):
        guest = (
            GuestAccount.objects.for_organization(organization)
            .filter(pk=guest_id)
            .first()
        )
        # Session points at a guest that no longer exists / was reassigned:
        # clear it so we don't keep re-querying a dead id every request.
        if guest is None:
            logout_guest(request)
    request._guest_cache = guest
    return guest


def build_login_link(guest, request):
    """The absolute magic-link URL that signs `guest` into the portal.

    `request` supplies the tenant host so the URL is correct in dev vs prod
    without hardcoding a domain (same approach as the ticket email). Shared by
    send_login_link (what we email) and the portal's SMTP-not-configured
    fallback (what we show on screen) so both mint the link the same way."""
    token = make_login_token(guest)
    return request.build_absolute_uri(f"{reverse('guest_verify')}?token={token}")


def email_delivery_configured():
    """Whether a sign-in email will actually reach the guest's inbox.

    The portal falls back to showing the magic link on screen when this is
    False -- the "SMTP is not set up yet" case, i.e. the prod SMTP backend is
    selected but EMAIL_HOST is still blank -- so a returning buyer isn't locked
    out of their tickets while mail delivery is being wired up. Any other
    backend (console/locmem/dummy in dev & tests, or a fully configured SMTP
    host in prod) is treated as "email works", and the flip happens
    automatically the moment EMAIL_HOST is set."""
    backend = getattr(settings, "EMAIL_BACKEND", "") or ""
    if backend.endswith("smtp.EmailBackend"):
        return bool(getattr(settings, "EMAIL_HOST", ""))
    return True


def send_login_link(guest, request):
    """Email `guest` a magic sign-in link for the portal. `request` supplies
    the tenant host so the absolute URL is correct in dev vs prod without
    hardcoding a domain (same approach as the ticket email). Uses the org's
    configured EMAIL_BACKEND; raises on transport failure so the caller can
    decide whether to surface it (the portal does, since the whole point of
    the request was to receive this email)."""
    link = build_login_link(guest, request)
    context = {
        "organization": guest.organization,
        "guest": guest,
        "login_link": link,
    }
    subject = f"Sign in to view your tickets — {guest.organization.name}"
    text_body = render_to_string("guests/email/login_link.txt", context)
    html_body = render_to_string("guests/email/login_link.html", context)

    email = EmailMultiAlternatives(
        subject=subject,
        body=text_body,
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=[guest.email],
    )
    email.attach_alternative(html_body, "text/html")
    email.send(fail_silently=False)
