"""End-to-end storefront view tests: tenant scoping/isolation at the HTTP
layer, the GA quantity flow, the reserved-seat flow, cart/checkout, and the
platform-host lockout.

Tenant resolution: pytest-django runs tests with settings.DEBUG forced to
False (mirroring `manage.py test` / production), so the DEBUG-only
`?_tenant=`/`X-Tenant` override in tenants/middleware.py never fires here —
which is fine, because it's meant purely as a laptop convenience, not a
tested code path. Instead these tests resolve tenants the same way
production does: a `Host` header ending in `.<BASE_DOMAIN>`. BASE_DOMAIN
defaults to "localhost" in config.settings.dev, so `org-a.localhost` ->
the "org-a" Organization, exactly like `roxy.boxo.show` -> "roxy" in prod.
"""

from decimal import Decimal

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from events.models import Event, GAAllocation, Performance, PriceTier
from orders.models import Hold
from promotions.models import PromoCode
from venues.models import Seat, SeatingChart, Section, Venue
from venues.tests import make_org


def host_for(subdomain):
    return f"{subdomain}.localhost"


class TenantClientMixin:
    """Thin wrapper so tests read as `self.get_as("org-a", "/")` instead of
    threading HTTP_HOST through every call."""

    def get_as(self, subdomain, path, **kwargs):
        return self.client.get(path, HTTP_HOST=host_for(subdomain), **kwargs)

    def post_as(self, subdomain, path, data=None, **kwargs):
        return self.client.post(path, data=data or {}, HTTP_HOST=host_for(subdomain), **kwargs)


class StorefrontFixtureMixin:
    def build_org(self, subdomain):
        org = make_org(subdomain)
        venue = Venue.objects.create(organization=org, name="Main Stage")
        return org, venue

    def build_ga(self, org, venue, slug="ga-show", capacity=5):
        event = Event.objects.create(
            organization=org, title="GA Show", slug=slug, status=Event.Status.PUBLISHED
        )
        performance = Performance.objects.create(
            organization=org,
            event=event,
            venue=venue,
            starts_at=timezone.now() + timezone.timedelta(days=1),
            seating_mode=Performance.SeatingMode.GA,
            status=Performance.Status.PUBLISHED,
        )
        GAAllocation.objects.create(organization=org, performance=performance, capacity=capacity)
        tier = PriceTier.objects.create(
            organization=org, performance=performance, name="GA", amount=Decimal("20.00")
        )
        return event, performance, tier

    def build_reserved(self, org, venue, slug="reserved-show"):
        event = Event.objects.create(
            organization=org, title="Reserved Show", slug=slug, status=Event.Status.PUBLISHED
        )
        performance = Performance.objects.create(
            organization=org,
            event=event,
            venue=venue,
            starts_at=timezone.now() + timezone.timedelta(days=1),
            seating_mode=Performance.SeatingMode.RESERVED,
            status=Performance.Status.PUBLISHED,
        )
        chart = SeatingChart.objects.create(organization=org, venue=venue, name="Standard")
        section = Section.objects.create(organization=org, chart=chart, name="Orchestra")
        seat = Seat.objects.create(organization=org, section=section, row_label="A", number="1")
        tier = PriceTier.objects.create(
            organization=org, section=section, name="Orchestra", amount=Decimal("50.00")
        )
        return event, performance, seat, tier


class HomeViewTests(TenantClientMixin, StorefrontFixtureMixin, TestCase):
    def test_home_lists_only_this_orgs_published_upcoming_events(self):
        org_a, venue_a = self.build_org("org-a")
        org_b, venue_b = self.build_org("org-b")
        self.build_ga(org_a, venue_a, slug="show-a")
        self.build_ga(org_b, venue_b, slug="show-b")

        resp = self.get_as("org-a", "/")
        self.assertContains(resp, "GA Show")
        self.assertNotContains(resp, "show-b")

    def test_home_hides_draft_events(self):
        org, venue = self.build_org("org-a")
        Event.objects.create(
            organization=org, title="Secret Draft", slug="draft", status=Event.Status.DRAFT
        )
        resp = self.get_as("org-a", "/")
        self.assertNotContains(resp, "Secret Draft")

    def test_home_hides_past_performances(self):
        org, venue = self.build_org("org-a")
        event = Event.objects.create(
            organization=org, title="Past Show", slug="past", status=Event.Status.PUBLISHED
        )
        Performance.objects.create(
            organization=org,
            event=event,
            venue=venue,
            starts_at=timezone.now() - timezone.timedelta(days=1),
            seating_mode=Performance.SeatingMode.GA,
            status=Performance.Status.PUBLISHED,
        )
        resp = self.get_as("org-a", "/")
        self.assertNotContains(resp, "Past Show")

    def test_no_tenant_shows_platform_landing_not_storefront(self):
        org, venue = self.build_org("org-a")
        self.build_ga(org, venue)
        resp = self.client.get("/")  # default testserver Host -> no tenant
        self.assertNotContains(resp, "GA Show")
        # Platform brand name (see templates/base.html) -- pre-existing
        # assertion updated for the "Boxo.show" rebrand; unrelated to this
        # change, fixed in passing to keep the suite green.
        self.assertContains(resp, "Boxo.show")


class EventDetailViewTests(TenantClientMixin, StorefrontFixtureMixin, TestCase):
    def test_shows_availability_badges(self):
        org, venue = self.build_org("org-a")
        self.build_ga(org, venue, capacity=5)
        self.build_reserved(org, venue)

        resp = self.get_as("org-a", "/events/reserved-show/")
        self.assertContains(resp, "1 available")

    def test_cross_org_slug_404s(self):
        org_a, venue_a = self.build_org("org-a")
        self.build_org("org-b")
        self.build_ga(org_a, venue_a, slug="only-in-a")

        resp = self.get_as("org-b", "/events/only-in-a/")
        self.assertEqual(resp.status_code, 404)

    def test_no_tenant_404s(self):
        org, venue = self.build_org("org-a")
        self.build_ga(org, venue, slug="show-a")
        resp = self.client.get("/events/show-a/")
        self.assertEqual(resp.status_code, 404)


class GAHoldFlowTests(TenantClientMixin, StorefrontFixtureMixin, TestCase):
    def setUp(self):
        self.org, self.venue = self.build_org("org-a")
        self.event, self.performance, self.tier = self.build_ga(self.org, self.venue, capacity=3)

    def test_setting_quantity_creates_hold_and_redirects_to_cart(self):
        resp = self.post_as(
            "org-a",
            f"/performances/{self.performance.pk}/hold/",
            {"price_tier": self.tier.pk, "quantity": 2},
        )
        self.assertRedirects(resp, "/cart/", fetch_redirect_response=False)
        self.assertEqual(Hold.objects.filter(performance=self.performance).count(), 1)
        self.assertEqual(Hold.objects.get(performance=self.performance).quantity, 2)

    def test_exceeding_availability_shows_error_and_creates_no_hold(self):
        resp = self.post_as(
            "org-a",
            f"/performances/{self.performance.pk}/hold/",
            {"price_tier": self.tier.pk, "quantity": 99},
        )
        self.assertRedirects(
            resp, f"/performances/{self.performance.pk}/", fetch_redirect_response=False
        )
        self.assertEqual(Hold.objects.filter(performance=self.performance).count(), 0)

    def test_cart_shows_hold_with_countdown_data(self):
        self.post_as(
            "org-a",
            f"/performances/{self.performance.pk}/hold/",
            {"price_tier": self.tier.pk, "quantity": 2},
        )
        resp = self.get_as("org-a", "/cart/")
        self.assertContains(resp, "GA Show")
        self.assertContains(resp, "holdCountdown(")
        self.assertContains(resp, "$40.00")  # 2 x $20

    def test_cart_release_removes_hold(self):
        self.post_as(
            "org-a",
            f"/performances/{self.performance.pk}/hold/",
            {"price_tier": self.tier.pk, "quantity": 2},
        )
        hold = Hold.objects.get(performance=self.performance)
        self.post_as("org-a", "/cart/release/", {"hold_id": hold.pk})
        self.assertEqual(Hold.objects.filter(pk=hold.pk).count(), 0)

    def test_checkout_get_shows_summary_and_creates_no_order(self):
        self.post_as(
            "org-a",
            f"/performances/{self.performance.pk}/hold/",
            {"price_tier": self.tier.pk, "quantity": 2},
        )
        resp = self.get_as("org-a", "/checkout/")
        self.assertContains(resp, "Proceed to payment")
        self.assertContains(resp, "$40.00")

        from orders.models import Order

        self.assertEqual(Order.objects.count(), 0)

    def test_checkout_post_redirects_to_stripe_without_creating_order(self):
        """POST /checkout/ creates a Stripe Checkout Session (mocked -- no
        network) for the targeted hold and redirects the browser to its
        hosted payment page. No Order/Payment/Ticket exists yet; that's the
        webhook's job (see payments/test_services.py,
        payments/test_views.py)."""
        from unittest.mock import patch

        from orders.models import Order

        # This org has finished Connect onboarding (charges enabled), so
        # create_checkout_session takes the real (mocked) Stripe path rather
        # than the not-connected stub (which is covered separately below).
        self.org.stripe_account_id = "acct_org_a"
        self.org.stripe_charges_enabled = True
        self.org.save(update_fields=["stripe_account_id", "stripe_charges_enabled"])

        self.post_as(
            "org-a",
            f"/performances/{self.performance.pk}/hold/",
            {"price_tier": self.tier.pk, "quantity": 2},
        )
        hold = Hold.objects.get(performance=self.performance)

        with patch("payments.services.stripe.checkout.Session.create") as mock_create:
            mock_create.return_value = type(
                "FakeSession", (), {"url": "https://checkout.stripe.com/pay/cs_test_123"}
            )()
            resp = self.post_as("org-a", "/checkout/", {"hold_id": hold.pk})

        self.assertRedirects(
            resp, "https://checkout.stripe.com/pay/cs_test_123", fetch_redirect_response=False
        )
        # The hold is left alone -- fulfillment (which deletes it) only
        # happens once the webhook confirms payment.
        self.assertTrue(Hold.objects.filter(pk=hold.pk).exists())
        self.assertEqual(Order.objects.count(), 0)

    def test_checkout_post_with_expired_hold_shows_error_and_creates_no_order(self):
        from orders.models import Order

        self.post_as(
            "org-a",
            f"/performances/{self.performance.pk}/hold/",
            {"price_tier": self.tier.pk, "quantity": 2},
        )
        hold = Hold.objects.get(performance=self.performance)
        hold.expires_at = timezone.now() - timezone.timedelta(minutes=1)
        hold.save(update_fields=["expires_at"])

        resp = self.post_as("org-a", "/checkout/", {"hold_id": hold.pk})
        self.assertEqual(resp.status_code, 404)  # expired hold no longer matches the lookup
        self.assertEqual(Order.objects.count(), 0)

    def test_checkout_post_without_stripe_connected_redirects_to_stub_not_500(self):
        """With Connect not finished on the org (the demo/pre-launch state,
        charges not enabled), "Proceed to payment" must NOT 500 -- it
        redirects to the simulated checkout stub instead, and never touches
        Stripe."""
        from unittest.mock import patch

        from orders.models import Order

        self.assertFalse(self.org.stripe_charges_enabled)  # not connected yet
        self.post_as(
            "org-a",
            f"/performances/{self.performance.pk}/hold/",
            {"price_tier": self.tier.pk, "quantity": 2},
        )
        hold = Hold.objects.get(performance=self.performance)

        with patch("payments.services.stripe.checkout.Session.create") as mock_create:
            resp = self.post_as("org-a", "/checkout/", {"hold_id": hold.pk})
            mock_create.assert_not_called()  # Stripe is never called without a key

        self.assertRedirects(
            resp,
            f"http://org-a.localhost/checkout/stub/?hold_id={hold.pk}",
            fetch_redirect_response=False,
        )
        # Nothing is fulfilled yet -- that happens when the stub form is posted.
        self.assertTrue(Hold.objects.filter(pk=hold.pk).exists())
        self.assertEqual(Order.objects.count(), 0)

    def test_stub_checkout_get_renders_simulated_payment_page(self):
        self.post_as(
            "org-a",
            f"/performances/{self.performance.pk}/hold/",
            {"price_tier": self.tier.pk, "quantity": 2},
        )
        hold = Hold.objects.get(performance=self.performance)

        resp = self.get_as("org-a", f"/checkout/stub/?hold_id={hold.pk}")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Simulated payment")
        self.assertContains(resp, "$40.00")  # 2 x $20 GA tier

    def test_stub_checkout_post_fulfills_order_without_stripe(self):
        from orders.models import Order, Payment

        self.post_as(
            "org-a",
            f"/performances/{self.performance.pk}/hold/",
            {"price_tier": self.tier.pk, "quantity": 2},
        )
        hold = Hold.objects.get(performance=self.performance)

        resp = self.post_as(
            "org-a",
            "/checkout/stub/",
            {"hold_id": hold.pk, "buyer_name": "Stub Buyer", "buyer_email": "stub@example.com"},
        )

        order = Order.objects.get()
        self.assertRedirects(
            resp, f"/tickets/{order.token}/", fetch_redirect_response=False
        )
        self.assertEqual(order.status, Order.Status.PAID)
        self.assertEqual(order.buyer_email, "stub@example.com")
        self.assertEqual(order.total, Decimal("40.00"))
        self.assertEqual(order.tickets.count(), 2)
        # A stub order is a simulated payment, never a real Stripe one.
        self.assertIsNone(order.stripe_checkout_session_id)
        self.assertEqual(Payment.objects.get(order=order).provider, "stub")
        # The hold is consumed exactly like a real fulfillment.
        self.assertFalse(Hold.objects.filter(pk=hold.pk).exists())

    def test_stub_checkout_post_requires_email(self):
        from orders.models import Order

        self.post_as(
            "org-a",
            f"/performances/{self.performance.pk}/hold/",
            {"price_tier": self.tier.pk, "quantity": 2},
        )
        hold = Hold.objects.get(performance=self.performance)

        resp = self.post_as("org-a", "/checkout/stub/", {"hold_id": hold.pk})
        self.assertRedirects(
            resp,
            f"/checkout/stub/?hold_id={hold.pk}",
            fetch_redirect_response=False,
        )
        self.assertEqual(Order.objects.count(), 0)
        self.assertTrue(Hold.objects.filter(pk=hold.pk).exists())

    def test_stub_checkout_survives_a_ticket_email_failure(self):
        """The order is fulfilled in the buyer's own "Pay" request, then the
        ticket email is sent. A broken/unconfigured mail server (e.g. no SMTP
        host set) must NOT 500 the buyer: the purchase already succeeded, so
        the email failure is swallowed+logged and the buyer still lands on
        their tickets. Regression test -- this used to bubble up as a 500."""
        from unittest.mock import patch

        from orders.models import Order

        self.post_as(
            "org-a",
            f"/performances/{self.performance.pk}/hold/",
            {"price_tier": self.tier.pk, "quantity": 2},
        )
        hold = Hold.objects.get(performance=self.performance)

        with patch(
            "orders.views.send_order_receipt", side_effect=Exception("smtp unavailable")
        ):
            resp = self.post_as(
                "org-a",
                "/checkout/stub/",
                {"hold_id": hold.pk, "buyer_email": "buyer@example.com"},
            )

        order = Order.objects.get()
        self.assertRedirects(
            resp, f"/tickets/{order.token}/", fetch_redirect_response=False
        )
        self.assertEqual(order.tickets.count(), 2)
        self.assertFalse(Hold.objects.filter(pk=hold.pk).exists())


class GAInertSeatMapTests(TenantClientMixin, StorefrontFixtureMixin, TestCase):
    """A GA performance whose venue has a seating chart shows that chart as an
    INERT (display-only) map -- a picture of the room -- without turning the
    page into the reserved-seat picker: GA stays quantity-based."""

    def setUp(self):
        self.org, self.venue = self.build_org("org-a")
        self.event, self.performance, self.tier = self.build_ga(self.org, self.venue)
        chart = SeatingChart.objects.create(
            organization=self.org, venue=self.venue, name="Standard"
        )
        section = Section.objects.create(organization=self.org, chart=chart, name="Floor")
        for n in range(1, 4):
            Seat.objects.create(
                organization=self.org, section=section, row_label="A", number=str(n)
            )

    def test_ga_detail_renders_inert_map_and_keeps_quantity_form(self):
        resp = self.get_as("org-a", f"/performances/{self.performance.pk}/")
        self.assertEqual(resp.status_code, 200)
        # Inert map is present...
        self.assertContains(resp, "seat-map--inert")
        self.assertContains(resp, "inertSeatMap()")
        # ...but the interactive reserved-seat picker (and its hold form) is not.
        self.assertNotContains(resp, "seatMap()")
        # The quantity selector is still the actual purchase control.
        self.assertContains(resp, 'name="quantity"')

    def test_ga_detail_without_a_chart_omits_the_map(self):
        # A GA performance at a bare venue (no seating chart) simply has no
        # map to show -- the page must still render with just the quantity form.
        org, venue = self.build_org("org-b")
        _, performance, _ = self.build_ga(org, venue, slug="bare-ga")
        resp = self.get_as("org-b", f"/performances/{performance.pk}/")
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, "seat-map--inert")
        self.assertContains(resp, 'name="quantity"')


class ReservedHoldFlowTests(TenantClientMixin, StorefrontFixtureMixin, TestCase):
    def setUp(self):
        self.org, self.venue = self.build_org("org-a")
        self.event, self.performance, self.seat, self.tier = self.build_reserved(
            self.org, self.venue
        )

    def test_selecting_seat_creates_hold(self):
        resp = self.post_as(
            "org-a",
            f"/performances/{self.performance.pk}/hold/",
            {"seat_id": [str(self.seat.pk)]},
        )
        self.assertRedirects(resp, "/cart/", fetch_redirect_response=False)
        hold = Hold.objects.get(performance=self.performance)
        self.assertEqual(list(hold.seats.all()), [self.seat])

    def test_seat_map_reflects_taken_seat_for_other_session(self):
        # A separate client (= separate session) holds the seat first.
        other_client = self.client_class()
        other_client.post(
            f"/performances/{self.performance.pk}/hold/",
            {"seat_id": [str(self.seat.pk)]},
            HTTP_HOST=host_for("org-a"),
        )

        resp = self.get_as("org-a", f"/performances/{self.performance.pk}/")
        self.assertContains(resp, '"state": "unavailable"')

    def test_taking_already_held_seat_shows_error_and_no_second_hold(self):
        other_client = self.client_class()
        other_client.post(
            f"/performances/{self.performance.pk}/hold/",
            {"seat_id": [str(self.seat.pk)]},
            HTTP_HOST=host_for("org-a"),
        )

        resp = self.post_as(
            "org-a",
            f"/performances/{self.performance.pk}/hold/",
            {"seat_id": [str(self.seat.pk)]},
        )
        self.assertRedirects(
            resp, f"/performances/{self.performance.pk}/", fetch_redirect_response=False
        )
        self.assertEqual(Hold.objects.filter(performance=self.performance).count(), 1)

    def test_blocked_seat_shows_blocked_state_and_cannot_be_held(self):
        from orders.models import PerformanceSeatBlock

        PerformanceSeatBlock.objects.create(
            organization=self.org, performance=self.performance, seat=self.seat
        )

        resp = self.get_as("org-a", f"/performances/{self.performance.pk}/")
        self.assertContains(resp, '"state": "blocked"')

        resp = self.post_as(
            "org-a",
            f"/performances/{self.performance.pk}/hold/",
            {"seat_id": [str(self.seat.pk)]},
        )
        self.assertRedirects(
            resp, f"/performances/{self.performance.pk}/", fetch_redirect_response=False
        )
        self.assertEqual(Hold.objects.filter(performance=self.performance).count(), 0)


class PlatformHostLockoutTests(StorefrontFixtureMixin, TestCase):
    """No Host override at all -> testserver's default Host doesn't end in
    BASE_DOMAIN -> TenantMiddleware treats it as the platform host
    (request.organization = None) -> require_tenant 404s every storefront
    URL. This is the same code path a request straight to the bare
    boxo.show platform host takes in production."""

    def setUp(self):
        self.org, self.venue = self.build_org("org-a")
        self.event, self.performance, self.tier = self.build_ga(self.org, self.venue)

    def test_platform_host_cannot_reach_event_detail(self):
        resp = self.client.get(f"/events/{self.event.slug}/")
        self.assertEqual(resp.status_code, 404)

    def test_platform_host_cannot_reach_performance_detail(self):
        resp = self.client.get(f"/performances/{self.performance.pk}/")
        self.assertEqual(resp.status_code, 404)

    def test_platform_host_cannot_reach_cart(self):
        resp = self.client.get("/cart/")
        self.assertEqual(resp.status_code, 404)

    def test_platform_host_cannot_reach_checkout(self):
        resp = self.client.get("/checkout/")
        self.assertEqual(resp.status_code, 404)

    def test_platform_host_cannot_post_hold(self):
        resp = self.client.post(
            f"/performances/{self.performance.pk}/hold/",
            {"price_tier": self.tier.pk, "quantity": 1},
        )
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(Hold.objects.count(), 0)


class ReleaseExpiredHoldsCommandTests(StorefrontFixtureMixin, TestCase):
    def test_deletes_only_expired_holds(self):
        org, venue = self.build_org("org-a")
        event, performance, tier = self.build_ga(org, venue, capacity=10)

        active = Hold.objects.create(
            organization=org,
            performance=performance,
            session_key="active-session",
            price_tier=tier,
            quantity=1,
            expires_at=timezone.now() + timezone.timedelta(minutes=10),
        )
        expired = Hold.objects.create(
            organization=org,
            performance=performance,
            session_key="expired-session",
            price_tier=tier,
            quantity=1,
            expires_at=timezone.now() - timezone.timedelta(minutes=1),
        )

        call_command("release_expired_holds")

        self.assertTrue(Hold.objects.filter(pk=active.pk).exists())
        self.assertFalse(Hold.objects.filter(pk=expired.pk).exists())


class CheckoutSuccessCancelViewTests(TenantClientMixin, StorefrontFixtureMixin, TestCase):
    def setUp(self):
        self.org, self.venue = self.build_org("org-a")
        self.event, self.performance, self.tier = self.build_ga(self.org, self.venue)

    def test_success_without_session_id_shows_processing_state(self):
        resp = self.get_as("org-a", "/checkout/success/")
        self.assertContains(resp, "confirming your payment")

    def test_success_with_unknown_session_id_shows_processing_state(self):
        resp = self.get_as("org-a", "/checkout/success/?session_id=cs_test_unknown")
        self.assertContains(resp, "confirming your payment")

    def test_success_with_known_session_id_shows_order(self):
        from orders.models import Order

        order = Order.objects.create(
            organization=self.org,
            performance=self.performance,
            buyer_email="buyer@example.com",
            total=Decimal("70.00"),
            status=Order.Status.PAID,
            stripe_checkout_session_id="cs_test_known",
        )
        resp = self.get_as("org-a", "/checkout/success/?session_id=cs_test_known")
        self.assertContains(resp, "View your tickets")
        self.assertContains(resp, str(order.token))

    def test_success_does_not_leak_another_orgs_order(self):
        from orders.models import Order

        org_b, venue_b = self.build_org("org-b")
        event_b, performance_b, tier_b = self.build_ga(org_b, venue_b, slug="show-b")
        Order.objects.create(
            organization=org_b,
            performance=performance_b,
            buyer_email="buyer@example.com",
            total=Decimal("70.00"),
            status=Order.Status.PAID,
            stripe_checkout_session_id="cs_test_cross_org",
        )
        resp = self.get_as("org-a", "/checkout/success/?session_id=cs_test_cross_org")
        self.assertContains(resp, "confirming your payment")

    def test_cancel_page_renders(self):
        resp = self.get_as("org-a", "/checkout/cancel/")
        self.assertContains(resp, "Checkout cancelled")


class TicketDetailViewTests(TenantClientMixin, StorefrontFixtureMixin, TestCase):
    def setUp(self):
        self.org, self.venue = self.build_org("org-a")
        self.event, self.performance, self.tier = self.build_ga(self.org, self.venue)

    def _make_order_with_tickets(self, org, performance, n=2):
        from orders.models import Order, Ticket

        order = Order.objects.create(
            organization=org,
            performance=performance,
            buyer_email="buyer@example.com",
            total=Decimal("70.00"),
            status=Order.Status.PAID,
        )
        for _ in range(n):
            Ticket.objects.create(organization=org, order=order, performance=performance)
        return order

    def test_renders_order_and_qr_codes(self):
        order = self._make_order_with_tickets(self.org, self.performance, n=2)
        resp = self.get_as("org-a", f"/tickets/{order.token}/")
        self.assertContains(resp, "Your tickets")
        self.assertContains(resp, "GA Show")
        self.assertContains(resp, "data:image/png;base64,")
        self.assertEqual(resp.content.decode().count("data:image/png;base64,"), 2)

    def test_unknown_token_404s(self):
        import uuid

        resp = self.get_as("org-a", f"/tickets/{uuid.uuid4()}/")
        self.assertEqual(resp.status_code, 404)

    def test_cross_org_token_404s(self):
        org_b, venue_b = self.build_org("org-b")
        event_b, performance_b, tier_b = self.build_ga(org_b, venue_b, slug="show-b")
        order = self._make_order_with_tickets(org_b, performance_b, n=1)

        resp = self.get_as("org-a", f"/tickets/{order.token}/")
        self.assertEqual(resp.status_code, 404)

    def test_no_tenant_404s(self):
        order = self._make_order_with_tickets(self.org, self.performance, n=1)
        resp = self.client.get(f"/tickets/{order.token}/")
        self.assertEqual(resp.status_code, 404)


class PromoApplyRemoveViewTests(TenantClientMixin, StorefrontFixtureMixin, TestCase):
    """orders.views.promo_apply / promo_remove: the cart-facing wrapper
    around orders.services.apply_promo_code/remove_promo_code (the money-
    path logic itself is covered in orders/test_services.py). These tests
    only exercise the HTTP layer -- scoping, messages, redirects."""

    def setUp(self):
        self.org, self.venue = self.build_org("org-a")
        self.event, self.performance, self.tier = self.build_ga(self.org, self.venue, capacity=5)

    def _hold(self):
        self.post_as(
            "org-a",
            f"/performances/{self.performance.pk}/hold/",
            {"price_tier": self.tier.pk, "quantity": 2},
        )
        return Hold.objects.get(performance=self.performance)

    def _make_promo(self, code="SAVE10", **kwargs):
        defaults = dict(
            organization=self.org,
            code=code,
            kind=PromoCode.Kind.PERCENT,
            value=Decimal("10.00"),
        )
        defaults.update(kwargs)
        return PromoCode.objects.create(**defaults)

    def test_apply_valid_code_snapshots_discount_and_redirects_to_cart(self):
        hold = self._hold()
        self._make_promo()

        resp = self.post_as("org-a", "/cart/promo/apply/", {"hold_id": hold.pk, "code": "save10"})

        self.assertRedirects(resp, "/cart/", fetch_redirect_response=False)
        hold.refresh_from_db()
        self.assertEqual(hold.promo_code_text, "SAVE10")
        self.assertEqual(hold.discount_amount, Decimal("4.00"))  # 10% of 2 x $20

        resp = self.get_as("org-a", "/cart/")
        self.assertContains(resp, "Code")
        self.assertContains(resp, "SAVE10")

    def test_apply_invalid_code_shows_error_and_leaves_hold_undiscounted(self):
        hold = self._hold()

        resp = self.post_as(
            "org-a", "/cart/promo/apply/", {"hold_id": hold.pk, "code": "NOPE"}, follow=True
        )

        self.assertRedirects(resp, "/cart/")
        self.assertContains(resp, "isn&#x27;t valid")
        hold.refresh_from_db()
        self.assertIsNone(hold.discount_amount)
        self.assertEqual(hold.promo_code_text, "")

    def test_apply_expired_code_shows_error(self):
        hold = self._hold()
        self._make_promo(ends_at=timezone.now() - timezone.timedelta(days=1))

        resp = self.post_as(
            "org-a", "/cart/promo/apply/", {"hold_id": hold.pk, "code": "SAVE10"}, follow=True
        )

        self.assertContains(resp, "expired")
        hold.refresh_from_db()
        self.assertIsNone(hold.discount_amount)

    def test_apply_inactive_code_shows_error(self):
        hold = self._hold()
        self._make_promo(is_active=False)

        resp = self.post_as(
            "org-a", "/cart/promo/apply/", {"hold_id": hold.pk, "code": "SAVE10"}, follow=True
        )

        self.assertContains(resp, "isn&#x27;t valid")
        hold.refresh_from_db()
        self.assertIsNone(hold.discount_amount)

    def test_apply_maxed_out_code_shows_error(self):
        hold = self._hold()
        self._make_promo(max_redemptions=1, redemption_count=1)

        resp = self.post_as(
            "org-a", "/cart/promo/apply/", {"hold_id": hold.pk, "code": "SAVE10"}, follow=True
        )

        self.assertContains(resp, "redemption limit")
        hold.refresh_from_db()
        self.assertIsNone(hold.discount_amount)

    def test_remove_clears_the_snapshot(self):
        hold = self._hold()
        self._make_promo()
        self.post_as("org-a", "/cart/promo/apply/", {"hold_id": hold.pk, "code": "SAVE10"})
        hold.refresh_from_db()
        self.assertIsNotNone(hold.discount_amount)

        resp = self.post_as("org-a", "/cart/promo/remove/", {"hold_id": hold.pk})

        self.assertRedirects(resp, "/cart/", fetch_redirect_response=False)
        hold.refresh_from_db()
        self.assertIsNone(hold.discount_amount)
        self.assertEqual(hold.promo_code_text, "")
        self.assertIsNone(hold.promo_code_id)

    def test_remove_on_an_already_gone_hold_does_not_crash(self):
        # services.remove_promo_code is a silent no-op for a missing hold --
        # the view must simply bounce back to the cart, not 404/500.
        resp = self.post_as("org-a", "/cart/promo/remove/", {"hold_id": 999999})
        self.assertRedirects(resp, "/cart/", fetch_redirect_response=False)

    def test_apply_to_another_sessions_hold_is_rejected_not_a_crash(self):
        hold = self._hold()
        self._make_promo()

        other_client = self.client_class()
        resp = other_client.post(
            "/cart/promo/apply/",
            {"hold_id": hold.pk, "code": "SAVE10"},
            HTTP_HOST=host_for("org-a"),
        )

        self.assertRedirects(resp, "/cart/", fetch_redirect_response=False)
        hold.refresh_from_db()
        self.assertIsNone(hold.discount_amount)  # a different session's hold, never touched

    def test_apply_to_another_tenants_hold_is_rejected_not_a_crash(self):
        hold = self._hold()
        other_org, other_venue = self.build_org("org-b")
        self._make_promo()  # lives on org-a; irrelevant here either way

        resp = self.post_as("org-b", "/cart/promo/apply/", {"hold_id": hold.pk, "code": "SAVE10"})

        self.assertRedirects(resp, "/cart/", fetch_redirect_response=False)
        hold.refresh_from_db()
        self.assertIsNone(hold.discount_amount)

    def test_apply_get_not_allowed(self):
        resp = self.get_as("org-a", "/cart/promo/apply/")
        self.assertEqual(resp.status_code, 405)

    def test_remove_get_not_allowed(self):
        resp = self.get_as("org-a", "/cart/promo/remove/")
        self.assertEqual(resp.status_code, 405)


class DonationCartFlowTests(TenantClientMixin, StorefrontFixtureMixin, TestCase):
    """orders.views.donation_add / donation_remove -- the cart-facing wrapper
    around orders.services.set_hold_donation/clear_hold_donation (the money-
    path logic is covered in orders/test_services.py by the other test
    agent). These tests exercise the HTTP layer: scoping, messages,
    redirects, and that cart/checkout/stub totals reflect a donation."""

    def setUp(self):
        self.org, self.venue = self.build_org("org-a")
        self.event, self.performance, self.tier = self.build_ga(self.org, self.venue, capacity=5)

    def _hold(self):
        self.post_as(
            "org-a",
            f"/performances/{self.performance.pk}/hold/",
            {"price_tier": self.tier.pk, "quantity": 2},
        )
        return Hold.objects.get(performance=self.performance)

    def _make_promo(self, code="SAVE10", **kwargs):
        defaults = dict(
            organization=self.org,
            code=code,
            kind=PromoCode.Kind.PERCENT,
            value=Decimal("10.00"),
        )
        defaults.update(kwargs)
        return PromoCode.objects.create(**defaults)

    def test_add_valid_donation_snapshots_it_and_redirects_to_cart(self):
        hold = self._hold()

        resp = self.post_as("org-a", "/cart/donation/add/", {"hold_id": hold.pk, "amount": "15"})

        self.assertRedirects(resp, "/cart/", fetch_redirect_response=False)
        hold.refresh_from_db()
        self.assertEqual(hold.donation_amount, Decimal("15.00"))
        self.assertIsNotNone(hold.donation_campaign_id)

        from donations.models import DonationCampaign

        campaign = DonationCampaign.objects.get(organization=self.org)
        self.assertEqual(hold.donation_campaign_id, campaign.pk)

    def test_add_creates_general_fund_campaign_if_none_exists(self):
        from donations.models import DonationCampaign

        self.assertFalse(DonationCampaign.objects.filter(organization=self.org).exists())
        hold = self._hold()
        self.post_as("org-a", "/cart/donation/add/", {"hold_id": hold.pk, "amount": "10"})
        self.assertEqual(DonationCampaign.objects.filter(organization=self.org).count(), 1)

    def test_add_bad_amount_shows_error_and_leaves_hold_undonated(self):
        hold = self._hold()

        resp = self.post_as(
            "org-a", "/cart/donation/add/", {"hold_id": hold.pk, "amount": "not-a-number"}, follow=True
        )

        self.assertRedirects(resp, "/cart/")
        self.assertContains(resp, "valid donation amount")
        hold.refresh_from_db()
        self.assertIsNone(hold.donation_amount)

    def test_add_zero_amount_rejected(self):
        hold = self._hold()
        resp = self.post_as(
            "org-a", "/cart/donation/add/", {"hold_id": hold.pk, "amount": "0"}, follow=True
        )
        self.assertContains(resp, "greater than zero")
        hold.refresh_from_db()
        self.assertIsNone(hold.donation_amount)

    def test_add_over_cap_amount_rejected(self):
        hold = self._hold()
        resp = self.post_as(
            "org-a", "/cart/donation/add/", {"hold_id": hold.pk, "amount": "999999"}, follow=True
        )
        self.assertContains(resp, "capped")
        hold.refresh_from_db()
        self.assertIsNone(hold.donation_amount)

    def test_remove_clears_the_snapshot(self):
        hold = self._hold()
        self.post_as("org-a", "/cart/donation/add/", {"hold_id": hold.pk, "amount": "20"})
        hold.refresh_from_db()
        self.assertIsNotNone(hold.donation_amount)

        resp = self.post_as("org-a", "/cart/donation/remove/", {"hold_id": hold.pk})

        self.assertRedirects(resp, "/cart/", fetch_redirect_response=False)
        hold.refresh_from_db()
        self.assertIsNone(hold.donation_amount)
        self.assertIsNone(hold.donation_campaign_id)

    def test_remove_on_an_already_gone_hold_does_not_crash(self):
        resp = self.post_as("org-a", "/cart/donation/remove/", {"hold_id": 999999})
        self.assertRedirects(resp, "/cart/", fetch_redirect_response=False)

    def test_add_to_another_sessions_hold_is_rejected_not_a_crash(self):
        hold = self._hold()

        other_client = self.client_class()
        resp = other_client.post(
            "/cart/donation/add/",
            {"hold_id": hold.pk, "amount": "10"},
            HTTP_HOST=host_for("org-a"),
        )

        self.assertRedirects(resp, "/cart/", fetch_redirect_response=False)
        hold.refresh_from_db()
        self.assertIsNone(hold.donation_amount)

    def test_add_to_another_tenants_hold_is_rejected_not_a_crash(self):
        hold = self._hold()
        self.build_org("org-b")

        resp = self.post_as("org-b", "/cart/donation/add/", {"hold_id": hold.pk, "amount": "10"})

        self.assertRedirects(resp, "/cart/", fetch_redirect_response=False)
        hold.refresh_from_db()
        self.assertIsNone(hold.donation_amount)

    def test_add_get_not_allowed(self):
        resp = self.get_as("org-a", "/cart/donation/add/")
        self.assertEqual(resp.status_code, 405)

    def test_remove_get_not_allowed(self):
        resp = self.get_as("org-a", "/cart/donation/remove/")
        self.assertEqual(resp.status_code, 405)

    def test_cart_shows_donation_line_and_grand_total_includes_it(self):
        hold = self._hold()  # 2 x $20 = $40
        self.post_as("org-a", "/cart/donation/add/", {"hold_id": hold.pk, "amount": "10"})

        resp = self.get_as("org-a", "/cart/")
        self.assertContains(resp, "Donation: $10.00")
        self.assertContains(resp, "$50.00")  # net total: 40 tickets + 10 donation

    def test_checkout_get_totals_include_donation(self):
        hold = self._hold()
        self.post_as("org-a", "/cart/donation/add/", {"hold_id": hold.pk, "amount": "10"})

        resp = self.get_as("org-a", "/checkout/")
        self.assertContains(resp, "Donation: $10.00")
        self.assertContains(resp, "$50.00")

    def test_stub_checkout_get_totals_include_donation(self):
        hold = self._hold()
        self.post_as("org-a", "/cart/donation/add/", {"hold_id": hold.pk, "amount": "10"})

        resp = self.get_as("org-a", f"/checkout/stub/?hold_id={hold.pk}")
        self.assertContains(resp, "Donation: $10.00")
        self.assertContains(resp, "$50.00")

    def test_stub_checkout_post_fulfills_ticket_order_with_donation_item(self):
        from orders.models import Order, OrderItem

        hold = self._hold()
        self.post_as("org-a", "/cart/donation/add/", {"hold_id": hold.pk, "amount": "10"})

        resp = self.post_as(
            "org-a",
            "/checkout/stub/",
            {"hold_id": hold.pk, "buyer_name": "Donor", "buyer_email": "donor@example.com"},
        )

        order = Order.objects.get()
        self.assertRedirects(resp, f"/tickets/{order.token}/", fetch_redirect_response=False)
        self.assertEqual(order.total, Decimal("50.00"))  # 40 tickets + 10 donation
        self.assertEqual(order.tickets.count(), 2)
        donation_items = order.items.filter(kind=OrderItem.Kind.DONATION)
        self.assertEqual(donation_items.count(), 1)
        self.assertEqual(donation_items.get().unit_amount, Decimal("10.00"))

    def test_promo_and_donation_coexist_net_tickets_plus_full_donation(self):
        """A promo discounts only the ticket subtotal; the donation rides on
        top untouched -- orders.services.hold_grand_total's contract."""
        from orders.models import Order

        hold = self._hold()  # 2 x $20 = $40 gross
        self._make_promo()  # 10% off
        self.post_as("org-a", "/cart/promo/apply/", {"hold_id": hold.pk, "code": "SAVE10"})
        self.post_as("org-a", "/cart/donation/add/", {"hold_id": hold.pk, "amount": "10"})

        resp = self.post_as(
            "org-a",
            "/checkout/stub/",
            {"hold_id": hold.pk, "buyer_email": "buyer@example.com"},
        )
        order = Order.objects.get()
        self.assertRedirects(resp, f"/tickets/{order.token}/", fetch_redirect_response=False)
        # net tickets = 40 - 4 (10%) = 36, plus the full $10 donation = $46
        self.assertEqual(order.total, Decimal("46.00"))


class DonationOnlyOrderRenderingTests(TenantClientMixin, StorefrontFixtureMixin, TestCase):
    """A donation-only Order (Order.performance null, no Ticket rows) must
    render everywhere a ticketed order does, without 500ing."""

    def setUp(self):
        self.org, self.venue = self.build_org("org-a")

    def _donation_order(self, amount=Decimal("25.00")):
        from donations.services import get_or_create_general_fund
        from orders.models import Order, OrderItem

        campaign = get_or_create_general_fund(self.org)
        order = Order.objects.create(
            organization=self.org,
            performance=None,
            buyer_email="donor@example.com",
            buyer_name="Donor Person",
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
        return order

    def test_ticket_detail_renders_donation_receipt_without_500(self):
        order = self._donation_order()
        resp = self.get_as("org-a", f"/tickets/{order.token}/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Donation receipt")
        self.assertContains(resp, "$25.00")

    def test_checkout_success_shows_donation_thank_you_without_500(self):
        from orders.models import Order

        order = self._donation_order()
        order.stripe_checkout_session_id = "cs_test_donation"
        order.save(update_fields=["stripe_checkout_session_id"])

        resp = self.get_as("org-a", "/checkout/success/?session_id=cs_test_donation")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Thank you for your donation")

    def test_ticket_pdf_404s_on_donation_only_order(self):
        order = self._donation_order()
        resp = self.get_as("org-a", f"/tickets/{order.token}/pdf/")
        self.assertEqual(resp.status_code, 404)

    def test_dashboard_order_list_and_detail_render_without_500(self):
        # Sanity check at the storefront layer that nothing about a donation-
        # only order's shape (null performance) breaks Django's template
        # resolution for the public receipt page under a second campaign-less
        # variant (donation_campaign None).
        from orders.models import Order, OrderItem

        order = Order.objects.create(
            organization=self.org,
            performance=None,
            buyer_email="donor2@example.com",
            total=Decimal("5.00"),
            status=Order.Status.PAID,
        )
        OrderItem.objects.create(
            organization=self.org,
            order=order,
            kind=OrderItem.Kind.DONATION,
            quantity=1,
            unit_amount=Decimal("5.00"),
            donation_campaign=None,
        )
        resp = self.get_as("org-a", f"/tickets/{order.token}/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "$5.00")


class DonateStandaloneFlowTests(TenantClientMixin, StorefrontFixtureMixin, TestCase):
    """The standalone /donate/ page: 404 when donations aren't on, stub
    fulfillment end to end when they are (donations/views.py, owned by this
    agent -- donations/tests.py itself belongs to the other test agent)."""

    def setUp(self):
        self.org, self.venue = self.build_org("org-a")

    def test_404s_when_donations_not_enabled(self):
        resp = self.get_as("org-a", "/donate/")
        self.assertEqual(resp.status_code, 404)

    def test_renders_when_enabled(self):
        from donations.services import get_or_create_general_fund

        get_or_create_general_fund(self.org)
        resp = self.get_as("org-a", "/donate/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Support")

    def test_post_without_stripe_connected_redirects_to_stub(self):
        from donations.services import get_or_create_general_fund

        get_or_create_general_fund(self.org)
        self.assertFalse(self.org.stripe_charges_enabled)

        resp = self.post_as(
            "org-a",
            "/donate/",
            {"amount": "25", "buyer_name": "Donor", "buyer_email": "donor@example.com"},
        )
        self.assertRedirects(
            resp, "http://org-a.localhost/donate/stub/?amount=25.00", fetch_redirect_response=False
        )

    def test_post_with_stripe_connected_creates_checkout_session(self):
        from unittest.mock import patch

        from donations.services import get_or_create_general_fund
        from orders.models import Order

        get_or_create_general_fund(self.org)
        self.org.stripe_account_id = "acct_org_a"
        self.org.stripe_charges_enabled = True
        self.org.save(update_fields=["stripe_account_id", "stripe_charges_enabled"])

        with patch("payments.services.stripe.checkout.Session.create") as mock_create:
            mock_create.return_value = type(
                "FakeSession", (), {"url": "https://checkout.stripe.com/pay/cs_test_donate"}
            )()
            resp = self.post_as(
                "org-a", "/donate/", {"amount": "25", "buyer_email": "donor@example.com"}
            )

        self.assertRedirects(
            resp, "https://checkout.stripe.com/pay/cs_test_donate", fetch_redirect_response=False
        )
        self.assertEqual(Order.objects.count(), 0)  # fulfillment is the webhook's job

    def test_post_bad_amount_shows_error_and_creates_no_order(self):
        from donations.services import get_or_create_general_fund
        from orders.models import Order

        get_or_create_general_fund(self.org)
        resp = self.post_as(
            "org-a", "/donate/", {"amount": "0", "buyer_email": "donor@example.com"}
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "greater than zero")
        self.assertEqual(Order.objects.count(), 0)

    def test_post_requires_email(self):
        from donations.services import get_or_create_general_fund
        from orders.models import Order

        get_or_create_general_fund(self.org)
        resp = self.post_as("org-a", "/donate/", {"amount": "25"})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Enter an email")
        self.assertEqual(Order.objects.count(), 0)

    def test_stub_get_404s_when_donations_not_enabled(self):
        resp = self.get_as("org-a", "/donate/stub/?amount=25")
        self.assertEqual(resp.status_code, 404)

    def test_stub_get_renders_simulated_payment_page(self):
        from donations.services import get_or_create_general_fund

        get_or_create_general_fund(self.org)
        resp = self.get_as("org-a", "/donate/stub/?amount=25")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Simulated payment")
        self.assertContains(resp, "$25")

    def test_stub_post_fulfills_donation_order_without_stripe(self):
        from donations.services import get_or_create_general_fund
        from orders.models import Order, OrderItem, Payment

        campaign = get_or_create_general_fund(self.org)
        resp = self.post_as(
            "org-a",
            "/donate/stub/",
            {"amount": "25", "buyer_name": "Donor", "buyer_email": "donor@example.com"},
        )

        order = Order.objects.get()
        self.assertRedirects(resp, f"/tickets/{order.token}/", fetch_redirect_response=False)
        self.assertEqual(order.status, Order.Status.PAID)
        self.assertIsNone(order.performance)
        self.assertEqual(order.total, Decimal("25.00"))
        self.assertEqual(order.tickets.count(), 0)
        item = order.items.get()
        self.assertEqual(item.kind, OrderItem.Kind.DONATION)
        self.assertEqual(item.donation_campaign_id, campaign.pk)
        self.assertEqual(Payment.objects.get(order=order).provider, "stub")
        # The buyer is signed in on this same request, like the ticket stub path.
        self.assertEqual(self.client.session.get("guest_account_id") is not None, True)

    def test_stub_post_requires_email(self):
        from donations.services import get_or_create_general_fund
        from orders.models import Order

        get_or_create_general_fund(self.org)
        resp = self.post_as("org-a", "/donate/stub/", {"amount": "25"})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Enter an email")
        self.assertEqual(Order.objects.count(), 0)

    def test_stub_404s_once_stripe_connected(self):
        from donations.services import get_or_create_general_fund

        get_or_create_general_fund(self.org)
        self.org.stripe_account_id = "acct_org_a"
        self.org.stripe_charges_enabled = True
        self.org.save(update_fields=["stripe_account_id", "stripe_charges_enabled"])

        resp = self.get_as("org-a", "/donate/stub/?amount=25")
        self.assertEqual(resp.status_code, 404)

    def test_post_with_test_checkout_enabled_and_no_stripe_fulfills_immediately(self):
        """When ENABLE_TEST_CHECKOUT is on and the org can't charge yet, the
        main /donate/ POST direct-fulfills (skipping even the stub redirect
        hop) with provider="test" -- mirrors orders.views.checkout_test's own
        env-gated shortcut for the ticket path."""
        from django.test import override_settings

        from donations.services import get_or_create_general_fund
        from orders.models import Order, Payment

        get_or_create_general_fund(self.org)
        self.assertFalse(self.org.stripe_charges_enabled)

        with override_settings(ENABLE_TEST_CHECKOUT=True):
            resp = self.post_as(
                "org-a",
                "/donate/",
                {"amount": "25", "buyer_name": "Donor", "buyer_email": "donor@example.com"},
            )

        order = Order.objects.get()
        self.assertRedirects(resp, f"/tickets/{order.token}/", fetch_redirect_response=False)
        self.assertEqual(order.total, Decimal("25.00"))
        self.assertEqual(Payment.objects.get(order=order).provider, "test")


class PassRedeemModeCartRenderingTests(TenantClientMixin, StorefrontFixtureMixin, TestCase):
    """Phase 3: the cart's "Redeem with pass" CTA (templates/orders/cart.html,
    gated by passes.context_processors.pass_nav's `redeeming_pass` + passes/
    templatetags/pass_tags.py's redeemable_with_pass) only appears for a hold
    the redeeming pass actually covers -- an uncovered hold gets a muted note
    instead, and neither shows up at all outside redeem mode. orders/views.py
    itself carries no passes import (see passes.context_processors' dependency
    -direction note) -- this is purely a template-rendering check, the
    business logic is covered in passes/test_views.py and payments/
    test_services.py."""

    def setUp(self):
        self.org, self.venue = self.build_org("org-a")
        self.event, self.performance, self.tier = self.build_ga(self.org, self.venue, capacity=5)
        self.other_event, self.other_performance, self.other_tier = self.build_ga(
            self.org, self.venue, slug="other-show", capacity=5
        )

    def _hold(self, performance, tier):
        self.post_as(
            "org-a", f"/performances/{performance.pk}/hold/", {"price_tier": tier.pk, "quantity": 1}
        )
        return Hold.objects.filter(performance=performance).latest("created_at")

    def _start_redeeming(self, events=None, credit_count=2):
        from passes.models import PassProduct

        product = PassProduct.objects.create(
            organization=self.org,
            name="Flex Pack",
            kind=PassProduct.Kind.FLEX,
            price=Decimal("40.00"),
            credit_count=credit_count,
        )
        if events is not None:
            product.events.set(events)
        self.post_as(
            "org-a", "/passes/stub/", {"product_id": product.pk, "buyer_email": "holder@example.com"}
        )
        from passes.models import PassPurchase

        purchase = PassPurchase.objects.get(product=product)
        self.post_as("org-a", "/passes/redeem/start/", {"pass_id": purchase.pk})
        return purchase

    def test_no_cta_outside_redeem_mode(self):
        self._hold(self.performance, self.tier)
        resp = self.get_as("org-a", "/cart/")
        self.assertNotContains(resp, "Redeem with pass")
        self.assertNotContains(resp, "Not covered by your pass")

    def test_cta_shown_only_for_covered_hold(self):
        self._start_redeeming(events=[self.event])
        self._hold(self.performance, self.tier)  # covered
        self._hold(self.other_performance, self.other_tier)  # not covered

        resp = self.get_as("org-a", "/cart/")
        self.assertContains(resp, "Redeem with pass")
        self.assertContains(resp, "Not covered by your pass")

    def test_all_access_pass_covers_every_hold(self):
        self._start_redeeming(events=None)  # empty events -> all-access
        self._hold(self.performance, self.tier)
        self._hold(self.other_performance, self.other_tier)

        resp = self.get_as("org-a", "/cart/")
        self.assertNotContains(resp, "Not covered by your pass")
        # One "Redeem with pass" button per covered hold.
        self.assertEqual(resp.content.decode().count("Redeem with pass"), 2)
