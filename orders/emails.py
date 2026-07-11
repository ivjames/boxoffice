"""Ticket confirmation email: HTML + text, one inline QR per ticket, and a
link to the public /tickets/<order-token>/ page. Sent by the Stripe webhook
handler (payments/services.py) right after the order-creating transaction
commits -- see that module's docstring for why email goes outside the
transaction. Uses Django's configured EMAIL_BACKEND (console in dev, SMTP in
prod -- config/settings/{dev,prod}.py), so nothing here is Stripe- or
transport-specific.
"""

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.urls import reverse

from .qr import ticket_qr_data_uri


def send_ticket_email(order, request):
    """Email `order.buyer_email` their tickets. `request` is the in-flight
    request that triggered fulfillment (the Stripe webhook POST, which lands
    on the tenant subdomain just like a browser request would) -- it's used
    to build the absolute URL for the tickets page so it's correct for dev
    vs. prod without hardcoding a host here. (The QR codes need no request --
    they encode a bare ticket code, not a URL; see orders/qr.py.)
    """
    tickets = list(order.tickets.select_related("seat", "seat__section").order_by("id"))
    ticket_rows = [{"ticket": ticket, "qr_data_uri": ticket_qr_data_uri(ticket)} for ticket in tickets]
    tickets_url = request.build_absolute_uri(reverse("ticket_detail", args=[order.token]))

    # Carry the theater's branding into the email so a buyer sees the venue
    # they bought from, not "Boxo.show". The palette lives on Organization
    # (same fields templates/base.html themes the storefront with); the logo
    # is an ImageField, so build an absolute URL from the in-flight request
    # (order.organization.logo.url is host-relative) and only when one is set.
    organization = order.organization
    logo_url = (
        request.build_absolute_uri(organization.logo.url) if organization.logo else None
    )

    context = {
        "order": order,
        "organization": organization,
        "logo_url": logo_url,
        "ticket_rows": ticket_rows,
        "tickets_url": tickets_url,
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
