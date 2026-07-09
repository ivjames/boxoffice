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
    to build absolute URLs for the QR codes and the tickets page so they're
    correct for dev vs. prod without hardcoding a host here.
    """
    tickets = list(order.tickets.select_related("seat", "seat__section").order_by("id"))
    ticket_rows = [{"ticket": ticket, "qr_data_uri": ticket_qr_data_uri(ticket, request)} for ticket in tickets]
    tickets_url = request.build_absolute_uri(reverse("ticket_detail", args=[order.token]))

    context = {
        "order": order,
        "organization": order.organization,
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
