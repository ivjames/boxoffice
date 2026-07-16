"""Order receipt emails: the ticket confirmation (HTML + text, one inline QR
per ticket) and -- Phase 2 -- the donation acknowledgment for a donation-only
order, plus `send_order_receipt`, the one dispatcher every caller should use
so they never have to know which kind of order they're emailing. Sent by the
Stripe webhook handler (payments/services.py) right after the order-creating
transaction commits -- see that module's docstring for why email goes
outside the transaction. Uses Django's configured EMAIL_BACKEND (console in
dev, SMTP in prod -- config/settings/{dev,prod}.py), so nothing here is
Stripe- or transport-specific.

Every absolute URL in these emails is derived from the order's Organization
(Organization.base_url), NOT from whatever request triggered the send: the
Connect webhook that fulfills real purchases arrives on the PLATFORM host,
where request-derived links would 404 on the tenant-gated routes they point
at. That's also why none of these functions take a request.
"""

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.urls import reverse

from tenants.logo_images import read_logo_bytes

from .models import OrderItem
from .qr import ticket_qr_data_uri


def send_order_receipt(order):
    """The ONE entry point callers should use to email an order's receipt --
    dispatches to send_ticket_email for an order with tickets, send_pass_
    purchase_email for a pass-purchase order (Phase 3), or send_donation_
    receipt_email for a donation-only order (Order.performance is null, no
    Ticket rows). Every fulfillment path (the Stripe webhook, the stub/test
    checkout "Pay" request, a staff resend) now calls this instead of
    send_ticket_email directly, so every order kind gets its email through the
    exact same call sites a ticket order's confirmation always has, with no
    per-caller branching.

    DISPATCH ORDER MATTERS -- tickets first, then PASS, then donation fallback:

      - Tickets first: any order that minted tickets gets the ticket email --
        including a PASS REDEMPTION order, which has real Tickets (a pass was
        spent on seats) and should send the buyer those seats, not a
        purchase-confirmation. `order.tickets.exists()` is the same "what does
        this email need to render" key the donation split already used.
      - PASS before the donation fallback: a pass PURCHASE order has NO tickets
        (like a donation-only order), so without an explicit PASS check it would
        fall through to the donation email. Checking for a kind=PASS line first
        routes it to the pass receipt instead. This ordering is why the pass
        branch is an `elif` ABOVE the donation `else`, not after it.
      - Donation last: the remaining ticketless, passless order is a
        donation-only gift.
    """
    if order.tickets.exists():
        send_ticket_email(order)
    elif order.items.filter(kind=OrderItem.Kind.PASS).exists():
        send_pass_purchase_email(order)
    else:
        send_donation_receipt_email(order)


def send_ticket_email(order):
    """Email `order.buyer_email` their tickets. Every absolute URL is built
    from the ORDER'S ORGANIZATION (Organization.base_url), never from the
    request that triggered fulfillment: the Stripe Connect webhook that
    fulfills real purchases is delivered to the PLATFORM host
    (boxo.show/webhooks/stripe/, see DEPLOY.md), so a request-derived link
    would point buyers at tenant-gated routes on the wrong host and 404.
    (The QR codes need no URL at all -- they encode a bare ticket code, not
    a link; see orders/qr.py.)
    """
    tickets = list(order.tickets.select_related("seat", "seat__section").order_by("id"))
    organization = order.organization
    # Read the org logo once and hand it to every ticket's QR (they share one
    # org) rather than re-reading the file per ticket; None => plain QR codes.
    logo_bytes = read_logo_bytes(organization)
    ticket_rows = [
        {"ticket": ticket, "qr_data_uri": ticket_qr_data_uri(ticket, logo_bytes=logo_bytes)}
        for ticket in tickets
    ]
    tickets_url = organization.base_url + reverse("ticket_detail", args=[order.token])

    # Carry the theater's branding into the email so a buyer sees the venue
    # they bought from, not "Boxo.show". The palette lives on Organization
    # (same fields templates/base.html themes the storefront with); the logo
    # is an ImageField whose .url is host-relative, so absolutize it on the
    # tenant origin, and only when one is set.
    logo_url = organization.base_url + organization.logo.url if organization.logo else None

    # Phase 2: a ticket order can ALSO carry a donation added at the cart
    # (orders.services.set_hold_donation) -- surface that as an extra line +
    # its campaign's acknowledgment blurb on the SAME ticket email, rather
    # than a second message. None when this order has no donation item (the
    # common case), which tickets.html/.txt treat as "omit the section".
    donation_item = (
        order.items.filter(kind=OrderItem.Kind.DONATION)
        .select_related("donation_campaign")
        .first()
    )

    context = {
        "order": order,
        "organization": organization,
        "logo_url": logo_url,
        "ticket_rows": ticket_rows,
        "tickets_url": tickets_url,
        "donation_item": donation_item,
    }
    subject = f"Your tickets for {order.performance.event.title} — {order.organization.name}"
    text_body = render_to_string("orders/email/tickets.txt", context)
    html_body = render_to_string("orders/email/tickets.html", context)

    email = EmailMultiAlternatives(
        subject=subject,
        body=text_body,
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=[order.buyer_email],
    )
    email.attach_alternative(html_body, "text/html")
    email.send(fail_silently=False)


def send_donation_receipt_email(order):
    """Email `order.buyer_email` their donation receipt -- the Phase 2
    analogue of send_ticket_email for a donation-only order (Order.performance
    null, no Ticket rows): no tickets/QR/performance to show, just the
    amount given, the campaign's nonprofit acknowledgment blurb (when set),
    and a link to the same public receipt page a ticket order gets
    (/tickets/<order.token>/ -- orders.views.ticket_detail already renders a
    donation-only order's receipt view, see its template's guard).

    The receipt URL is built from the order's organization, exactly like
    send_ticket_email -- never from the fulfilling request, which may be the
    platform-host webhook."""
    donation_item = (
        order.items.filter(kind=OrderItem.Kind.DONATION)
        .select_related("donation_campaign")
        .first()
    )
    campaign = donation_item.donation_campaign if donation_item is not None else None
    amount = donation_item.unit_amount if donation_item is not None else order.total

    organization = order.organization
    receipt_url = organization.base_url + reverse("ticket_detail", args=[order.token])
    logo_url = organization.base_url + organization.logo.url if organization.logo else None

    context = {
        "order": order,
        "organization": organization,
        "logo_url": logo_url,
        "campaign": campaign,
        "amount": amount,
        "receipt_url": receipt_url,
    }
    subject = f"Thank you for your donation — {organization.name}"
    text_body = render_to_string("orders/email/donation_receipt.txt", context)
    html_body = render_to_string("orders/email/donation_receipt.html", context)

    email = EmailMultiAlternatives(
        subject=subject,
        body=text_body,
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=[order.buyer_email],
    )
    email.attach_alternative(html_body, "text/html")
    email.send(fail_silently=False)


def send_pass_purchase_email(order):
    """Email `order.buyer_email` their pass receipt -- the Phase 3 analogue of
    send_donation_receipt_email for a pass PURCHASE order (no tickets: the pass
    is redeemed for seats LATER, through the guest portal). Renders the pass
    product's name, a plain-language explanation of what the pass grants (N
    credits for a flex pass, one admission per show for a season pass), its
    valid window when set, and a link to the guest portal (/account/) where the
    holder redeems it -- with the same org branding the other receipts carry.

    Reads the pass detail off the PassPurchase issued for this order (its frozen
    snapshots), falling back to the kind=PASS OrderItem for the product name/
    amount if the purchase row can't be found (it always can in practice -- both
    are created in the same fulfill_pass_purchase transaction). The portal URL
    is built from the order's organization, exactly like the other receipt
    senders -- never from the fulfilling request, which may be the
    platform-host webhook."""
    pass_item = (
        order.items.filter(kind=OrderItem.Kind.PASS)
        .select_related("pass_product")
        .first()
    )
    purchase = order.pass_purchases.first()

    product = pass_item.pass_product if pass_item is not None else None
    product_name = (
        product.name if product is not None else (purchase.product.name if purchase else "Pass")
    )
    amount = pass_item.unit_amount if pass_item is not None else order.total

    # Portal is where the holder redeems -- named route lands with the guests
    # app; absolutized on the tenant origin like the other senders.
    organization = order.organization
    portal_url = organization.base_url + reverse("guest_portal")
    logo_url = organization.base_url + organization.logo.url if organization.logo else None

    context = {
        "order": order,
        "organization": organization,
        "logo_url": logo_url,
        "purchase": purchase,
        "product_name": product_name,
        "amount": amount,
        "portal_url": portal_url,
    }
    subject = f"Your {product_name} — {organization.name}"
    text_body = render_to_string("orders/email/pass_purchase.txt", context)
    html_body = render_to_string("orders/email/pass_purchase.html", context)

    email = EmailMultiAlternatives(
        subject=subject,
        body=text_body,
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=[order.buyer_email],
    )
    email.attach_alternative(html_body, "text/html")
    email.send(fail_silently=False)
