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

        # This org HAS Stripe configured, so create_checkout_session takes the
        # real (mocked) Stripe path rather than the no-keys stub (which is
        # covered separately below).
        self.org.stripe_secret_key = "sk_test_org_a_secret"
        self.org.save(update_fields=["stripe_secret_key"])

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

    def test_checkout_post_without_stripe_keys_redirects_to_stub_not_500(self):
        """With no Stripe keys on the org (the demo/pre-launch state),
        "Proceed to payment" must NOT 500 on a Stripe auth error -- it
        redirects to the simulated checkout stub instead, and never touches
        Stripe."""
        from unittest.mock import patch

        from orders.models import Order

        self.assertEqual(self.org.stripe_secret_key, "")  # no Stripe configured
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
            "orders.views.send_ticket_email", side_effect=Exception("smtp unavailable")
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
