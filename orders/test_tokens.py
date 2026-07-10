"""Tests for orders/tokens.py: the HMAC scheme Phase 5's scanner will reuse
to verify a scanned ticket QR's signature."""

import uuid
from decimal import Decimal

from django.test import RequestFactory, TestCase

from orders import tokens
from orders.models import Order, Ticket
from orders.tests import OrdersFixtureMixin
from venues.tests import make_org


class TicketSigningTests(OrdersFixtureMixin, TestCase):
    def setUp(self):
        self.build_ga_performance()
        order = Order.objects.create(
            organization=self.org,
            performance=self.performance,
            buyer_email="buyer@example.com",
            total=Decimal("35.00"),
        )
        self.ticket = Ticket.objects.create(organization=self.org, order=order, performance=self.performance)

    def test_sign_and_verify_round_trip(self):
        sig = tokens.sign_ticket(self.ticket)
        self.assertTrue(tokens.verify_ticket_sig(self.ticket.token, sig, self.ticket.organization_id))

    def test_tampered_token_fails(self):
        sig = tokens.sign_ticket(self.ticket)
        self.assertFalse(tokens.verify_ticket_sig(uuid.uuid4(), sig, self.ticket.organization_id))

    def test_tampered_signature_fails(self):
        sig = tokens.sign_ticket(self.ticket)
        flipped_char = "0" if sig[-1] != "0" else "1"
        tampered = sig[:-1] + flipped_char
        self.assertFalse(tokens.verify_ticket_sig(self.ticket.token, tampered, self.ticket.organization_id))

    def test_missing_signature_fails(self):
        self.assertFalse(tokens.verify_ticket_sig(self.ticket.token, "", self.ticket.organization_id))
        self.assertFalse(tokens.verify_ticket_sig(self.ticket.token, None, self.ticket.organization_id))

    def test_signature_is_scoped_per_organization(self):
        other_org = make_org("org-b")
        sig = tokens.sign_ticket(self.ticket)  # signed under self.org's key
        self.assertFalse(tokens.verify_ticket_sig(self.ticket.token, sig, other_org.id))

    def test_scan_path_carries_token_and_signature_as_segments(self):
        path = tokens.scan_path(self.ticket)
        # token and sig are consecutive path segments, not a ?sig= query param.
        self.assertEqual(path, f"/S/{self.ticket.token}/{tokens.sign_ticket(self.ticket)}/")
        self.assertNotIn("?", path)

    def test_build_ticket_scan_url_is_absolute_uppercase_and_host_correct(self):
        request = RequestFactory().get("/", HTTP_HOST="roxy.localhost")
        url = tokens.build_ticket_scan_url(self.ticket, request)
        # Whole URL is uppercased to stay in QR alphanumeric mode.
        self.assertTrue(url.startswith("HTTP://ROXY.LOCALHOST/S/"))
        self.assertEqual(url, url.upper())
        self.assertIn(self.ticket.token, url)  # token is already uppercase base32
