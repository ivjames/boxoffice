"""Tests for guest (ticket-buyer) accounts: the email-keyed GuestAccount
model, magic-link tokens, order linking at fulfillment, the self-service
portal (sign-in / list / verify / logout), email capture during selection,
and the ticket PDF download. Tenant isolation is checked throughout -- a
guest, a signed-in session, and a magic link are all scoped to one org.

Reuses orders.test_views' fixtures so the storefront shape (org, GA
performance, tier) is built exactly the same way the rest of the suite
builds it.
"""

from decimal import Decimal

from django.core import mail
from django.test import TestCase, override_settings
from django.utils import timezone

from orders.models import Hold, Order
from orders.test_views import StorefrontFixtureMixin, TenantClientMixin
from passes.models import PassProduct, PassPurchase
from payments.services import fulfill_hold_with_pass, fulfill_pass_purchase

from . import services
from .models import GuestAccount, normalize_email
from .tokens import make_login_token, read_login_token


class GuestAccountModelTests(StorefrontFixtureMixin, TestCase):
    def setUp(self):
        self.org, self.venue = self.build_org("org-a")

    def test_get_or_create_normalizes_email(self):
        g1, created1 = GuestAccount.objects.get_or_create_for_email(
            self.org, "  Buyer@Example.COM "
        )
        self.assertTrue(created1)
        self.assertEqual(g1.email, "buyer@example.com")

        # Same address, different casing/whitespace -> same account.
        g2, created2 = GuestAccount.objects.get_or_create_for_email(
            self.org, "buyer@example.com"
        )
        self.assertFalse(created2)
        self.assertEqual(g1.pk, g2.pk)

    def test_blank_email_yields_no_account(self):
        guest, created = GuestAccount.objects.get_or_create_for_email(self.org, "")
        self.assertIsNone(guest)
        self.assertFalse(created)
        self.assertEqual(GuestAccount.objects.count(), 0)

    def test_name_backfilled_when_first_seen_without_one(self):
        guest, _ = GuestAccount.objects.get_or_create_for_email(self.org, "a@b.com")
        self.assertEqual(guest.name, "")
        GuestAccount.objects.get_or_create_for_email(self.org, "a@b.com", name="Ada")
        guest.refresh_from_db()
        self.assertEqual(guest.name, "Ada")

    def test_same_email_distinct_across_orgs(self):
        org_b, _ = self.build_org("org-b")
        ga, _ = GuestAccount.objects.get_or_create_for_email(self.org, "x@y.com")
        gb, _ = GuestAccount.objects.get_or_create_for_email(org_b, "x@y.com")
        self.assertNotEqual(ga.pk, gb.pk)

    def test_normalize_email_helper(self):
        self.assertEqual(normalize_email("  A@B.Com "), "a@b.com")
        self.assertEqual(normalize_email(None), "")


class MagicLinkTokenTests(StorefrontFixtureMixin, TestCase):
    def setUp(self):
        self.org, _ = self.build_org("org-a")
        self.guest, _ = GuestAccount.objects.get_or_create_for_email(self.org, "g@x.com")

    def test_roundtrip(self):
        token = make_login_token(self.guest)
        self.assertEqual(read_login_token(token, self.org), self.guest.pk)

    def test_rejected_for_other_org(self):
        org_b, _ = self.build_org("org-b")
        token = make_login_token(self.guest)
        self.assertIsNone(read_login_token(token, org_b))

    def test_expired_rejected(self):
        token = make_login_token(self.guest)
        self.assertIsNone(read_login_token(token, self.org, max_age=-1))

    def test_tampered_rejected(self):
        token = make_login_token(self.guest) + "x"
        self.assertIsNone(read_login_token(token, self.org))

    def test_garbage_rejected(self):
        self.assertIsNone(read_login_token("not-a-token", self.org))
        self.assertIsNone(read_login_token("", self.org))


@override_settings(ENABLE_TEST_CHECKOUT=True)
class FulfillmentLinksGuestTests(TenantClientMixin, StorefrontFixtureMixin, TestCase):
    """Fulfillment (exercised via the TEST CHECKOUT HTTP path) must create/
    link a GuestAccount and sign the buyer in on that same request."""

    def setUp(self):
        self.org, self.venue = self.build_org("org-a")
        self.event, self.performance, self.tier = self.build_ga(self.org, self.venue, capacity=10)

    def _buy(self, email, name="Buyer", quantity=1):
        self.post_as(
            "org-a",
            f"/performances/{self.performance.pk}/hold/",
            {"price_tier": self.tier.pk, "quantity": quantity},
        )
        hold = Hold.objects.filter(performance=self.performance).latest("created_at")
        return self.post_as(
            "org-a",
            "/checkout/test/",
            {"hold_id": hold.pk, "buyer_email": email, "buyer_name": name},
        )

    def test_order_linked_to_guest_and_buyer_signed_in(self):
        self._buy("buyer@example.com")
        order = Order.objects.get()
        guest = GuestAccount.objects.get()
        self.assertEqual(guest.email, "buyer@example.com")
        self.assertEqual(order.guest_id, guest.pk)
        # Buyer is signed in on this session for their guest account.
        self.assertEqual(self.client.session.get("guest_account_id"), guest.pk)
        self.assertEqual(self.client.session.get("guest_org_id"), self.org.pk)

    def test_second_order_same_email_reuses_account(self):
        self._buy("buyer@example.com")
        self._buy("Buyer@Example.com")  # different casing
        self.assertEqual(GuestAccount.objects.count(), 1)
        guest = GuestAccount.objects.get()
        self.assertEqual(Order.objects.filter(guest=guest).count(), 2)


@override_settings(ENABLE_TEST_CHECKOUT=True)
class GuestPortalTests(TenantClientMixin, StorefrontFixtureMixin, TestCase):
    def setUp(self):
        self.org, self.venue = self.build_org("org-a")
        self.event, self.performance, self.tier = self.build_ga(self.org, self.venue, capacity=10)

    def _buy(self, email):
        self.post_as(
            "org-a",
            f"/performances/{self.performance.pk}/hold/",
            {"price_tier": self.tier.pk, "quantity": 1},
        )
        hold = Hold.objects.filter(performance=self.performance).latest("created_at")
        return self.post_as(
            "org-a", "/checkout/test/", {"hold_id": hold.pk, "buyer_email": email}
        )

    def test_portal_shows_signin_form_when_signed_out(self):
        resp = self.get_as("org-a", "/account/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Email me a sign-in link")

    def test_portal_lists_orders_after_purchase(self):
        self._buy("buyer@example.com")  # auto-signs in
        resp = self.get_as("org-a", "/account/")
        self.assertContains(resp, "GA Show")
        self.assertContains(resp, "Download PDF")

    def test_request_link_sends_email_when_account_exists(self):
        self._buy("buyer@example.com")
        # Sign out so we exercise the magic-link request path.
        self.post_as("org-a", "/account/logout/")
        mail.outbox.clear()

        resp = self.post_as("org-a", "/account/link/", {"email": "buyer@example.com"})
        self.assertRedirects(resp, "/account/", fetch_redirect_response=False)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["buyer@example.com"])

    def test_request_link_no_email_when_no_account(self):
        mail.outbox.clear()
        resp = self.post_as("org-a", "/account/link/", {"email": "nobody@example.com"})
        self.assertRedirects(resp, "/account/", fetch_redirect_response=False)
        self.assertEqual(len(mail.outbox), 0)  # anti-enumeration: silent

    @override_settings(
        EMAIL_BACKEND="django.core.mail.backends.smtp.EmailBackend", EMAIL_HOST=""
    )
    def test_request_link_shows_link_on_page_when_smtp_unconfigured(self):
        """SMTP not set up yet: rather than send an email that goes nowhere,
        the portal renders the magic link on screen so the buyer can still
        reach their tickets."""
        self._buy("buyer@example.com")
        self.post_as("org-a", "/account/logout/")
        mail.outbox.clear()

        resp = self.post_as("org-a", "/account/link/", {"email": "buyer@example.com"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(mail.outbox), 0)  # nothing emailed
        self.assertContains(resp, "/account/verify/?token=")

        # And the shown link actually signs the guest in.
        guest = GuestAccount.objects.get(email="buyer@example.com")
        token = make_login_token(guest)
        self.assertContains(resp, token)

    @override_settings(
        EMAIL_BACKEND="django.core.mail.backends.smtp.EmailBackend", EMAIL_HOST=""
    )
    def test_request_link_no_link_shown_for_unknown_email_when_smtp_unconfigured(self):
        mail.outbox.clear()
        resp = self.post_as("org-a", "/account/link/", {"email": "nobody@example.com"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(mail.outbox), 0)
        self.assertNotContains(resp, "/account/verify/?token=")

    def test_email_delivery_configured_detects_smtp_setup(self):
        """The on-screen fallback keys off email_delivery_configured(): only
        an SMTP backend with no host counts as "not set up". A host (or any
        non-SMTP backend, like the locmem one tests use) flips it back to the
        emailed, anti-enumeration path automatically."""
        smtp = "django.core.mail.backends.smtp.EmailBackend"
        with override_settings(EMAIL_BACKEND=smtp, EMAIL_HOST=""):
            self.assertFalse(services.email_delivery_configured())
        with override_settings(EMAIL_BACKEND=smtp, EMAIL_HOST="smtp.example.com"):
            self.assertTrue(services.email_delivery_configured())
        with override_settings(
            EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend"
        ):
            self.assertTrue(services.email_delivery_configured())

    def test_verify_signs_guest_in(self):
        guest, _ = GuestAccount.objects.get_or_create_for_email(self.org, "buyer@example.com")
        token = make_login_token(guest)
        resp = self.get_as("org-a", f"/account/verify/?token={token}")
        self.assertRedirects(resp, "/account/", fetch_redirect_response=False)
        self.assertEqual(self.client.session.get("guest_account_id"), guest.pk)

    def test_verify_bad_token_does_not_sign_in(self):
        resp = self.get_as("org-a", "/account/verify/?token=bogus")
        self.assertRedirects(resp, "/account/", fetch_redirect_response=False)
        self.assertIsNone(self.client.session.get("guest_account_id"))

    def test_logout_clears_session(self):
        self._buy("buyer@example.com")
        self.assertIsNotNone(self.client.session.get("guest_account_id"))
        self.post_as("org-a", "/account/logout/")
        self.assertIsNone(self.client.session.get("guest_account_id"))

    def test_guest_session_not_honored_on_other_tenant(self):
        """A session signed in on org-a must not resolve a guest on org-b."""
        org_b, venue_b = self.build_org("org-b")
        self._buy("buyer@example.com")  # signs in on org-a
        # Hitting org-b's portal with the same client cookie shows the signed
        # -out form, not org-a's guest.
        resp = self.get_as("org-b", "/account/")
        self.assertContains(resp, "Email me a sign-in link")

    def test_portal_404_on_platform_host(self):
        resp = self.client.get("/account/", HTTP_HOST="localhost")
        self.assertEqual(resp.status_code, 404)


@override_settings(ENABLE_TEST_CHECKOUT=True)
class EmailCaptureDuringSelectionTests(TenantClientMixin, StorefrontFixtureMixin, TestCase):
    def setUp(self):
        self.org, self.venue = self.build_org("org-a")
        self.event, self.performance, self.tier = self.build_ga(self.org, self.venue, capacity=10)

    def test_email_captured_on_hold_prefills_checkout(self):
        self.post_as(
            "org-a",
            f"/performances/{self.performance.pk}/hold/",
            {"price_tier": self.tier.pk, "quantity": 1, "buyer_email": "early@example.com"},
        )
        self.assertEqual(self.client.session.get("guest_email"), "early@example.com")
        # The captured email pre-fills the checkout form.
        resp = self.get_as("org-a", "/checkout/")
        self.assertContains(resp, "early@example.com")


@override_settings(ENABLE_TEST_CHECKOUT=True)
class TicketPdfTests(TenantClientMixin, StorefrontFixtureMixin, TestCase):
    def setUp(self):
        self.org, self.venue = self.build_org("org-a")
        self.event, self.performance, self.tier = self.build_ga(self.org, self.venue, capacity=10)

    def _buy(self, email="buyer@example.com"):
        self.post_as(
            "org-a",
            f"/performances/{self.performance.pk}/hold/",
            {"price_tier": self.tier.pk, "quantity": 2},
        )
        hold = Hold.objects.filter(performance=self.performance).latest("created_at")
        self.post_as("org-a", "/checkout/test/", {"hold_id": hold.pk, "buyer_email": email})
        return Order.objects.get()

    def test_pdf_download(self):
        order = self._buy()
        resp = self.get_as("org-a", f"/tickets/{order.token}/pdf/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Content-Type"], "application/pdf")
        self.assertIn("attachment", resp["Content-Disposition"])
        self.assertTrue(resp.content.startswith(b"%PDF"))

    def test_pdf_scoped_to_tenant(self):
        order = self._buy()
        org_b, _ = self.build_org("org-b")
        resp = self.get_as("org-b", f"/tickets/{order.token}/pdf/")
        self.assertEqual(resp.status_code, 404)


class DonationOnlyOrderPortalTests(TenantClientMixin, StorefrontFixtureMixin, TestCase):
    """Phase 2: the guest portal must render a donation-only order (no
    performance, no tickets) as its own row, without 500ing -- see
    templates/guests/portal.html's guard."""

    def setUp(self):
        self.org, self.venue = self.build_org("org-a")

    def _donation_order(self, email="donor@example.com", amount=Decimal("20.00")):
        from donations.services import get_or_create_general_fund
        from orders.models import OrderItem

        campaign = get_or_create_general_fund(self.org)
        guest, _ = GuestAccount.objects.get_or_create_for_email(self.org, email)
        order = Order.objects.create(
            organization=self.org,
            performance=None,
            buyer_email=email,
            guest=guest,
            total=amount,
            status=Order.Status.PAID,
        )
        OrderItem.objects.create(
            organization=self.org,
            order=order,
            kind=OrderItem.Kind.DONATION,
            quantity=1,
            unit_amount=amount,
            donation_campaign=campaign,
        )
        return order, guest

    def test_portal_renders_donation_only_order_without_500(self):
        order, guest = self._donation_order()

        # Sign in the same way the storefront does (session write) --
        # mirrors guests.services.login_guest, which needs a real request
        # object rather than the test client.
        session = self.client.session
        session["guest_account_id"] = guest.pk
        session["guest_org_id"] = self.org.pk
        session.save()

        resp = self.get_as("org-a", "/account/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Donation")
        self.assertContains(resp, "$20.00")
        self.assertContains(resp, "View receipt")

    def test_portal_mixes_ticket_and_donation_orders(self):
        # A ticket order (normal row) alongside a donation-only order (the
        # new row shape) for the same signed-in guest.
        from orders.models import Order as OrderModel
        from orders.models import Ticket as TicketModel

        _event, performance, tier = self.build_ga(self.org, self.venue)
        guest, _ = GuestAccount.objects.get_or_create_for_email(self.org, "buyer@example.com")
        ticket_order = OrderModel.objects.create(
            organization=self.org,
            performance=performance,
            buyer_email="buyer@example.com",
            guest=guest,
            total=Decimal("20.00"),
            status=OrderModel.Status.PAID,
        )
        TicketModel.objects.create(organization=self.org, order=ticket_order, performance=performance)

        from donations.services import get_or_create_general_fund
        from orders.models import OrderItem

        campaign = get_or_create_general_fund(self.org)
        donation_order = OrderModel.objects.create(
            organization=self.org,
            performance=None,
            buyer_email="buyer@example.com",
            guest=guest,
            total=Decimal("5.00"),
            status=OrderModel.Status.PAID,
        )
        OrderItem.objects.create(
            organization=self.org,
            order=donation_order,
            kind=OrderItem.Kind.DONATION,
            quantity=1,
            unit_amount=Decimal("5.00"),
            donation_campaign=campaign,
        )

        session = self.client.session
        session["guest_account_id"] = guest.pk
        session["guest_org_id"] = self.org.pk
        session.save()

        resp = self.get_as("org-a", "/account/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "GA Show")  # the ticket order
        self.assertContains(resp, "$5.00")  # the donation order


class GuestPortalMyPassesTests(TenantClientMixin, StorefrontFixtureMixin, TestCase):
    """Phase 3: the guest portal's "My passes" section (guests/views.py's
    guest_portal + templates/guests/portal.html) -- active and exhausted
    passes both render, each with the right remaining/redeemable state, and
    the redeem button only appears when redeemable_now is True."""

    def setUp(self):
        self.org, self.venue = self.build_org("org-a")
        self.event, self.performance, self.tier = self.build_ga(self.org, self.venue, capacity=10)

    def _sign_in(self, guest):
        session = self.client.session
        session["guest_account_id"] = guest.pk
        session["guest_org_id"] = self.org.pk
        session.save()

    def _make_product(self, kind=PassProduct.Kind.FLEX, **kwargs):
        defaults = dict(name="Flex 3-Pack", kind=kind, price=Decimal("30.00"), organization=self.org)
        if kind == PassProduct.Kind.FLEX:
            defaults["credit_count"] = kwargs.pop("credit_count", 3)
        defaults.update(kwargs)
        return PassProduct.objects.create(**defaults)

    def test_no_passes_shows_empty_state_and_buy_link(self):
        guest, _ = GuestAccount.objects.get_or_create_for_email(self.org, "buyer@example.com")
        self._sign_in(guest)
        resp = self.get_as("org-a", "/account/")
        self.assertContains(resp, "My passes")
        self.assertContains(resp, "No passes on this account yet")
        self.assertContains(resp, "/passes/")

    def test_active_flex_pass_shows_remaining_and_redeem_button(self):
        product = self._make_product(credit_count=3)
        order = fulfill_pass_purchase(
            self.org,
            product=product,
            buyer_email="buyer@example.com",
            buyer_name="Buyer",
            provider="stub",
            payment_ref="stub-1",
        )
        self._sign_in(order.guest)

        resp = self.get_as("org-a", "/account/")
        self.assertContains(resp, product.name)
        self.assertContains(resp, "3 remaining")
        self.assertContains(resp, "Active")
        self.assertContains(resp, "Redeem")

    def test_all_access_season_pass_shows_unlimited(self):
        product = self._make_product(kind=PassProduct.Kind.SEASON)  # no events -> all-access
        order = fulfill_pass_purchase(
            self.org,
            product=product,
            buyer_email="season@example.com",
            buyer_name="Season Holder",
            provider="stub",
            payment_ref="stub-2",
        )
        self._sign_in(order.guest)

        resp = self.get_as("org-a", "/account/")
        self.assertContains(resp, "Unlimited shows")

    def test_exhausted_pass_shows_status_and_no_redeem_button(self):
        product = self._make_product(credit_count=1)
        order = fulfill_pass_purchase(
            self.org,
            product=product,
            buyer_email="buyer@example.com",
            buyer_name="Buyer",
            provider="stub",
            payment_ref="stub-3",
        )
        purchase = PassPurchase.objects.get(order=order)
        hold = Hold.objects.create(
            organization=self.org,
            performance=self.performance,
            price_tier=self.tier,
            quantity=1,
            session_key="exhaust-session",
        )
        fulfill_hold_with_pass(hold, purchase, buyer_email="buyer@example.com", buyer_name="Buyer")
        purchase.refresh_from_db()
        self.assertEqual(purchase.status, PassPurchase.Status.EXHAUSTED)

        self._sign_in(order.guest)
        resp = self.get_as("org-a", "/account/")
        self.assertContains(resp, "Exhausted")
        self.assertNotContains(resp, "Redeem<")

    def test_other_guests_pass_not_shown(self):
        product = self._make_product()
        fulfill_pass_purchase(
            self.org,
            product=product,
            buyer_email="someone-else@example.com",
            buyer_name="",
            provider="stub",
            payment_ref="stub-4",
        )
        guest, _ = GuestAccount.objects.get_or_create_for_email(self.org, "buyer@example.com")
        self._sign_in(guest)
        resp = self.get_as("org-a", "/account/")
        self.assertContains(resp, "No passes on this account yet")
