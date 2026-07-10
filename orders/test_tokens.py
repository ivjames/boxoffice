"""Tests for orders/tokens.py: the HMAC scheme Phase 5's scanner will reuse
to verify a scanned ticket QR's signature."""

import uuid
from decimal import Decimal

from django.test import TestCase

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

    def test_scan_code_is_token_dot_signature(self):
        code = tokens.scan_code(self.ticket)
        # Bare "<token>.<sig>" -- no URL, scheme, host, or query string.
        self.assertEqual(code, f"{self.ticket.token}.{tokens.sign_ticket(self.ticket)}")
        self.assertNotIn("/", code)
        self.assertNotIn("?", code)

    def test_scan_code_is_all_uppercase_alphanumeric(self):
        # Stays inside QR alphanumeric mode: uppercase A-Z / 0-9 and the '.' split
        # (token uses the unambiguous alphabet; sig is base32 -- both are A-Z0-9).
        code = tokens.scan_code(self.ticket)
        self.assertRegex(code, r"^[A-Z0-9]+\.[A-Z0-9]+$")

    def test_scan_code_round_trips_through_verify(self):
        # A scanner can split the code and verify the ticket from it alone.
        token, sig = tokens.scan_code(self.ticket).split(".")
        self.assertEqual(token, self.ticket.token)
        self.assertTrue(tokens.verify_ticket_sig(token, sig, self.ticket.organization_id))

    def test_new_token_avoids_ambiguous_characters(self):
        from orders.models import new_token

        # The look-alikes a hand-typed code must never contain (each dropped in
        # favour of its distinct counterpart, or outright -- see orders.models):
        # 0 O, 1 I L, Z, S, 6, B, U.
        forbidden = set("0O1ILZS6BU")
        sample = "".join(new_token() for _ in range(500))
        offenders = sorted(set(sample) & forbidden)
        self.assertEqual(offenders, [], f"token contained ambiguous chars: {offenders}")
        self.assertRegex(sample, r"^[A-Z0-9]+$")  # still uppercase alphanumeric
