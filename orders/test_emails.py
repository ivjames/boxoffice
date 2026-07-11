"""Tests for orders/emails.py: the ticket confirmation email sent after a
webhook-fulfilled order. Django's test runner swaps EMAIL_BACKEND for the
in-memory backend automatically, so these never touch a real mail server."""

from decimal import Decimal

from django.core import mail
from django.test import RequestFactory, TestCase

from orders.emails import send_ticket_email
from orders.models import Order, Ticket
from orders.tests import OrdersFixtureMixin


class SendTicketEmailTests(OrdersFixtureMixin, TestCase):
    def setUp(self):
        self.build_ga_performance()
        self.order = Order.objects.create(
            organization=self.org,
            performance=self.performance,
            buyer_email="buyer@example.com",
            buyer_name="Buyer Person",
            total=Decimal("70.00"),
            status=Order.Status.PAID,
        )
        Ticket.objects.create(
            organization=self.org, order=self.order, performance=self.performance, holder_name="Buyer Person"
        )
        Ticket.objects.create(
            organization=self.org, order=self.order, performance=self.performance, holder_name="Buyer Person"
        )
        self.request = RequestFactory().get("/", HTTP_HOST=f"{self.org.subdomain}.localhost")

    def test_sends_one_email_with_html_and_text_parts(self):
        send_ticket_email(self.order, self.request)

        self.assertEqual(len(mail.outbox), 1)
        email = mail.outbox[0]
        self.assertEqual(email.to, ["buyer@example.com"])
        self.assertIn(self.order.performance.event.title, email.subject)
        self.assertIn(str(self.order.token), email.body)

        self.assertEqual(len(email.alternatives), 1)
        html_body, mimetype = email.alternatives[0]
        self.assertEqual(mimetype, "text/html")
        # One inline QR per ticket.
        self.assertEqual(html_body.count("data:image/png;base64,"), 2)

    def test_html_email_uses_the_tenants_palette(self):
        self.org.primary_color = "#0d3b66"
        self.org.accent_color = "#f4a261"
        self.org.save(update_fields=["primary_color", "accent_color"])

        send_ticket_email(self.order, self.request)

        html_body = mail.outbox[0].alternatives[0][0]
        self.assertIn("#0d3b66", html_body)
        self.assertIn("#f4a261", html_body)

    def test_no_logo_falls_back_to_the_org_name(self):
        # No logo uploaded on the fixture org -> the header shows the name,
        # and there's no broken <img> pointing at an empty ImageField URL.
        send_ticket_email(self.order, self.request)

        html_body = mail.outbox[0].alternatives[0][0]
        self.assertIn(self.org.name, html_body)
        self.assertNotIn('src=""', html_body)


class SendTicketEmailReservedSeatTests(OrdersFixtureMixin, TestCase):
    """Separate class/setUp from SendTicketEmailTests: that class's setUp
    already builds a GA fixture under subdomain "roxy", and
    OrdersFixtureMixin.build_reserved_performance() would try to create a
    second Organization with the same subdomain in the same test."""

    def setUp(self):
        self.build_reserved_performance()
        self.request = RequestFactory().get("/", HTTP_HOST=f"{self.org.subdomain}.localhost")

    def test_reserved_ticket_email_mentions_seat(self):
        order = Order.objects.create(
            organization=self.org,
            performance=self.performance,
            buyer_email="seatbuyer@example.com",
            total=Decimal("65.00"),
            status=Order.Status.PAID,
        )
        Ticket.objects.create(organization=self.org, order=order, performance=self.performance, seat=self.seat)

        send_ticket_email(order, self.request)

        email = mail.outbox[-1]
        self.assertIn("A1", email.body)
