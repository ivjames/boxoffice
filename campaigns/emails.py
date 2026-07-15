"""Campaign email rendering + delivery.

This is the cron-side counterpart to guests.services.send_login_link, with one
crucial difference: there is NO request object here. Campaign emails are sent by
a management command (campaigns.management.commands.send_campaign_emails) running
under cron, so we can't lean on request.build_absolute_uri to make links
absolute. tenant_base_url reconstructs the storefront's origin from the org's
subdomain + settings.BASE_DOMAIN instead, so the unsubscribe link in the email
points at the right theater's host in dev and prod alike.

Templates (templates/campaigns/email/campaign.{txt,html}) are authored by the
UI layer; the context keys this module passes are the FROZEN contract between
the two: organization, logo_url, campaign, guest, body, unsubscribe_url. `body`
is pre-rendered (HTML: linebreaks applied) so the templates just drop it in.

Every send carries List-Unsubscribe / List-Unsubscribe-Post headers so Gmail/
Apple Mail surface a native one-click unsubscribe -- a deliverability
requirement for bulk senders, and the RFC 8058 companion to the in-body link.
"""

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.utils.html import linebreaks

from guests.tokens import make_unsubscribe_token


def tenant_base_url(organization):
    """The absolute origin (scheme + host, no trailing slash) of
    `organization`'s storefront -- e.g. "https://roxy.boxo.show".

    Thin alias for Organization.base_url, kept for this module's existing
    callers; the logic moved onto the model when orders/emails.py needed the
    same host derivation (the Connect webhook's request is for the platform
    host, so emailed receipt links must be rebuilt from the org instead)."""
    return organization.base_url


def render_campaign(campaign, guest, *, unsubscribe_url):
    """Render `campaign` for `guest` into (subject, text_body, html_body).

    The subject is the campaign's as-composed. Both bodies come from the shared
    templates under templates/campaigns/email/ with the FROZEN context keys:
    organization, logo_url, campaign, guest, body, unsubscribe_url. `body` is
    the campaign's plain text passed straight to the .txt template and, for the
    HTML part, run through Django's `linebreaks` so the paragraphs the staffer
    typed survive as <p>/<br> without them having to author any HTML.

    logo_url is absolute (built off the tenant origin) when the org has a logo,
    else "" -- the template shows the org name when there's no logo."""
    base_url = tenant_base_url(campaign.organization)
    logo_url = ""
    if getattr(campaign.organization, "logo", None):
        # MEDIA_URL is a relative path in dev; make the logo absolute off the
        # tenant origin so it loads inside a mail client with no page context.
        logo_url = f"{base_url}{campaign.organization.logo.url}"

    context = {
        "organization": campaign.organization,
        "logo_url": logo_url,
        "campaign": campaign,
        "guest": guest,
        "body": campaign.body,
        "unsubscribe_url": unsubscribe_url,
    }
    html_context = dict(context, body=linebreaks(campaign.body))

    subject = campaign.subject
    text_body = render_to_string("campaigns/email/campaign.txt", context)
    html_body = render_to_string("campaigns/email/campaign.html", html_context)
    return subject, text_body, html_body


def send_campaign_send(campaign_send, *, unsubscribe_url=None):
    """Render and send the ONE email a CampaignSend row represents.

    Renders the campaign for this row's guest, then sends a single
    EmailMultiAlternatives from settings.DEFAULT_FROM_EMAIL to the snapshotted
    address (campaign_send.email -- where the campaign was queued to, not a live
    re-read of guest.email). Attaches the List-Unsubscribe /
    List-Unsubscribe-Post headers (RFC 8058 one-click) alongside the in-body
    link so mail clients surface a native unsubscribe button.

    fail_silently=False on purpose: the batch sender catches the exception and
    records it on the CampaignSend row (status=FAILED, error=...), so a bad send
    must RAISE here rather than being swallowed -- silent success would strand a
    row PENDING/SENDING forever.

    `unsubscribe_url` is normally passed in by the batch sender (which mints it
    per guest -- see send_campaign_emails), so the SAME signed link goes in both
    the body and the List-Unsubscribe header. When omitted (a direct caller),
    it's built here off the guest so this stays self-contained/testable."""
    campaign = campaign_send.campaign
    guest = campaign_send.guest
    if unsubscribe_url is None:
        unsubscribe_url = tenant_base_url(campaign.organization) + _unsubscribe_path(guest)

    subject, text_body, html_body = render_campaign(
        campaign, guest, unsubscribe_url=unsubscribe_url
    )

    email = EmailMultiAlternatives(
        subject=subject,
        body=text_body,
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=[campaign_send.email],
        headers={
            "List-Unsubscribe": f"<{unsubscribe_url}>",
            "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
        },
    )
    email.attach_alternative(html_body, "text/html")
    email.send(fail_silently=False)


def send_test_campaign_email(campaign, to_email):
    """Send a one-off PREVIEW of `campaign` to a staff address, WITHOUT creating
    any CampaignSend row or touching the campaign's status.

    Used by the composer's "send me a test" action so a staffer can see the real
    rendered email before triggering the bulk send. Renders against the first
    opted-in guest in the segment if there is one (so merge fields look real),
    else a synthetic in-memory GuestAccount -- either way the unsubscribe link is
    a genuine signed token (for the sample guest, or a harmless synthetic link
    when there's no guest yet), so the preview matches production exactly. Sends
    to `to_email`, not the guest."""
    # Import here to avoid a models<-services<-emails import cycle at module load
    # (services imports nothing from emails; emails only needs the segment query
    # for this preview convenience).
    from guests.models import GuestAccount

    from . import services

    guest = services.segment_guests(campaign).first()
    if guest is None:
        # No opted-in recipient yet: render against a synthetic, unsaved guest so
        # the preview still works.
        guest = GuestAccount(
            organization=campaign.organization,
            email=to_email,
            name="Sample Subscriber",
        )
    # A test send ALWAYS uses a non-mutating placeholder unsubscribe link, never a
    # real guest's signed token. The message goes to the staffer, but its footer
    # link (and List-Unsubscribe header) would otherwise carry a live one-click
    # token for whichever real subscriber seeded the preview -- so the staffer's
    # click, or a mail scanner that fetches links, would silently opt out a
    # customer who never asked. The placeholder points at the bare unsubscribe
    # page (no token -> renders the "invalid link" branch, opts out nobody).
    unsubscribe_url = tenant_base_url(campaign.organization) + "/account/unsubscribe/"

    subject, text_body, html_body = render_campaign(
        campaign, guest, unsubscribe_url=unsubscribe_url
    )

    email = EmailMultiAlternatives(
        subject=f"[TEST] {subject}",
        body=text_body,
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=[to_email],
        headers={
            "List-Unsubscribe": f"<{unsubscribe_url}>",
            "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
        },
    )
    email.attach_alternative(html_body, "text/html")
    email.send(fail_silently=False)


def _unsubscribe_path(guest):
    """The unsubscribe URL path + signed token for `guest`.

    Reverses the UI-layer route named "guest_unsubscribe" (added with the
    campaigns views) and appends the signed token. Imported/reversed lazily here
    so this module stays importable before that URL exists; the batch sender
    likewise references the name only at run time."""
    from django.urls import reverse

    token = make_unsubscribe_token(guest)
    return f"{reverse('guest_unsubscribe')}?token={token}"
