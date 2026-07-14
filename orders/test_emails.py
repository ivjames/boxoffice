"""Tests for orders/emails.py: the ticket confirmation email sent after a
webhook-fulfilled order, the Phase 2 donation receipt, and the
send_order_receipt dispatcher between them. Django's test runner swaps
EMAIL_BACKEND for the in-memory backend automatically, so these never touch a
real mail server."""

from decimal import Decimal
from unittest.mock import patch

from django.core import mail
from django.test import RequestFactory, TestCase

from donations.services import get_or_create_general_fund
from orders.emails import send_donation_receipt_email, send_order_receipt, send_ticket_email
from orders.models import Order, OrderItem, Ticket
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


class SendDonationReceiptEmailTests(OrdersFixtureMixin, TestCase):
    """Phase 2: the donation-only receipt email (no tickets/QR/performance)."""

    def setUp(self):
        self.build_ga_performance()  # gives us self.org on subdomain "roxy"
        self.campaign = get_or_create_general_fund(self.org)
        self.campaign.acknowledgment = (
            "The Roxy is a 501(c)(3); no goods or services were provided in "
            "exchange for this contribution."
        )
        self.campaign.save(update_fields=["acknowledgment"])
        self.order = Order.objects.create(
            organization=self.org,
            performance=None,
            buyer_email="donor@example.com",
            buyer_name="Generous Donor",
            total=Decimal("50.00"),
            status=Order.Status.PAID,
        )
        OrderItem.objects.create(
            organization=self.org,
            order=self.order,
            kind=OrderItem.Kind.DONATION,
            quantity=1,
            unit_amount=Decimal("50.00"),
            donation_campaign=self.campaign,
        )
        self.request = RequestFactory().get("/", HTTP_HOST=f"{self.org.subdomain}.localhost")

    def test_sends_receipt_with_amount_and_blurb_no_qr(self):
        send_donation_receipt_email(self.order, self.request)

        self.assertEqual(len(mail.outbox), 1)
        email = mail.outbox[0]
        self.assertEqual(email.to, ["donor@example.com"])
        self.assertIn("donation", email.subject.lower())
        self.assertIn("50.00", email.body)
        self.assertIn("501(c)(3)", email.body)
        self.assertIn(str(self.order.token), email.body)

        html_body, mimetype = email.alternatives[0]
        self.assertEqual(mimetype, "text/html")
        self.assertIn("501(c)(3)", html_body)
        # No tickets on a donation-only order -- no QR codes to embed.
        self.assertNotIn("data:image/png;base64,", html_body)

    def test_no_campaign_omits_the_blurb_without_crashing(self):
        self.order.items.update(donation_campaign=None)
        send_donation_receipt_email(self.order, self.request)
        self.assertEqual(len(mail.outbox), 1)
        self.assertNotIn("501(c)(3)", mail.outbox[0].body)


class OrderReceiptDispatcherTests(OrdersFixtureMixin, TestCase):
    """orders.emails.send_order_receipt: routes a ticketed order to
    send_ticket_email and a donation-only order to send_donation_receipt_email."""

    def setUp(self):
        self.build_ga_performance()
        self.campaign = get_or_create_general_fund(self.org)
        self.request = RequestFactory().get("/", HTTP_HOST=f"{self.org.subdomain}.localhost")

    def test_ticketed_order_dispatches_to_ticket_email(self):
        order = Order.objects.create(
            organization=self.org,
            performance=self.performance,
            buyer_email="buyer@example.com",
            total=Decimal("35.00"),
            status=Order.Status.PAID,
        )
        Ticket.objects.create(organization=self.org, order=order, performance=self.performance)

        with patch("orders.emails.send_ticket_email") as mock_ticket, patch(
            "orders.emails.send_donation_receipt_email"
        ) as mock_donation:
            send_order_receipt(order, self.request)

        mock_ticket.assert_called_once_with(order, self.request)
        mock_donation.assert_not_called()

    def test_donation_only_order_dispatches_to_donation_email(self):
        order = Order.objects.create(
            organization=self.org,
            performance=None,
            buyer_email="donor@example.com",
            total=Decimal("15.00"),
            status=Order.Status.PAID,
        )
        OrderItem.objects.create(
            organization=self.org,
            order=order,
            kind=OrderItem.Kind.DONATION,
            quantity=1,
            unit_amount=Decimal("15.00"),
            donation_campaign=self.campaign,
        )

        with patch("orders.emails.send_ticket_email") as mock_ticket, patch(
            "orders.emails.send_donation_receipt_email"
        ) as mock_donation:
            send_order_receipt(order, self.request)

        mock_donation.assert_called_once_with(order, self.request)
        mock_ticket.assert_not_called()

    def test_mixed_order_ticket_email_mentions_the_donation(self):
        """A ticket purchase with a donation added at the cart is still a
        TICKET email (it has tickets/QR to show) -- but that email now also
        surfaces the donation line + campaign blurb (see send_ticket_email's
        donation_item context)."""
        self.campaign.acknowledgment = "Thank you for supporting the Roxy!"
        self.campaign.save(update_fields=["acknowledgment"])

        order = Order.objects.create(
            organization=self.org,
            performance=self.performance,
            buyer_email="buyer@example.com",
            total=Decimal("45.00"),
            status=Order.Status.PAID,
        )
        Ticket.objects.create(organization=self.org, order=order, performance=self.performance)
        OrderItem.objects.create(
            organization=self.org,
            order=order,
            kind=OrderItem.Kind.DONATION,
            quantity=1,
            unit_amount=Decimal("10.00"),
            donation_campaign=self.campaign,
        )

        send_order_receipt(order, self.request)

        self.assertEqual(len(mail.outbox), 1)
        email = mail.outbox[0]
        self.assertIn("donation", email.body.lower())
        self.assertIn("10.00", email.body)
        self.assertIn("Thank you for supporting the Roxy!", email.body)
        html_body = email.alternatives[0][0]
        self.assertIn("Thank you for supporting the Roxy!", html_body)
        # Still has the QR for the one real ticket.
        self.assertIn("data:image/png;base64,", html_body)
