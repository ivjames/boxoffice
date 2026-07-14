"""Tests for the promotions foundation service layer (promotions/services.py):
lookup (get_usable_code), validation (validate_code), discount math
(compute_discount), and redemption accounting (record_redemption). These are
the hold-AGNOSTIC core every apply path routes through -- see the module and
PromoCode docstrings. The Hold-bound apply/remove flow is tested in
orders/test_services.py; the Stripe/fulfillment money path in
payments/test_services.py.
"""

from datetime import timedelta
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone

from promotions import services
from promotions.models import PromoCode
from promotions.services import PromoError
from venues.tests import make_org


def make_promo(org, code="SUMMER10", *, kind=PromoCode.Kind.PERCENT, value="10", **kwargs):
    return PromoCode.objects.create(
        organization=org, code=code, kind=kind, value=Decimal(value), **kwargs
    )


class ComputeDiscountTests(TestCase):
    """Pure discount math -- no usability judgment (that's validate_code)."""

    def setUp(self):
        self.org = make_org("roxy")

    def test_percent_of_round_amount(self):
        promo = make_promo(self.org, kind=PromoCode.Kind.PERCENT, value="10")
        self.assertEqual(services.compute_discount(promo, Decimal("65.00")), Decimal("6.50"))

    def test_percent_rounds_half_up_to_cents(self):
        # 33.33 * 15% = 4.9995 -> ROUND_HALF_UP to 2dp -> 5.00.
        promo = make_promo(self.org, kind=PromoCode.Kind.PERCENT, value="15")
        self.assertEqual(services.compute_discount(promo, Decimal("33.33")), Decimal("5.00"))

    def test_fixed_below_subtotal_is_taken_verbatim(self):
        promo = make_promo(self.org, kind=PromoCode.Kind.FIXED, value="10")
        self.assertEqual(services.compute_discount(promo, Decimal("65.00")), Decimal("10.00"))

    def test_fixed_over_subtotal_is_capped_at_subtotal(self):
        # compute_discount alone caps at the cart (min(value, subtotal)); note
        # validate_code separately REJECTS a discount >= subtotal (would zero
        # the cart) -- see ValidateCodeTests.test_discount_that_zeroes_cart_rejected.
        promo = make_promo(self.org, kind=PromoCode.Kind.FIXED, value="100")
        self.assertEqual(services.compute_discount(promo, Decimal("65.00")), Decimal("65.00"))

    def test_result_is_two_decimal_quantized(self):
        promo = make_promo(self.org, kind=PromoCode.Kind.PERCENT, value="15")
        self.assertEqual(
            services.compute_discount(promo, Decimal("33.33")).as_tuple().exponent, -2
        )


class ValidateCodeTests(TestCase):
    """Every rejection path plus the happy path. Time-window checks use fixed,
    timezone-aware datetimes passed as `now` so they don't depend on the clock."""

    def setUp(self):
        self.org = make_org("roxy")
        self.now = timezone.now()

    def test_happy_path_passes_silently(self):
        promo = make_promo(self.org, kind=PromoCode.Kind.PERCENT, value="10")
        self.assertIsNone(
            services.validate_code(promo, subtotal=Decimal("65.00"), currency="USD", now=self.now)
        )

    def test_inactive_rejected(self):
        promo = make_promo(self.org, value="10", is_active=False)
        with self.assertRaises(PromoError):
            services.validate_code(promo, subtotal=Decimal("65.00"), currency="USD", now=self.now)

    def test_before_start_window_rejected(self):
        promo = make_promo(self.org, value="10", starts_at=self.now + timedelta(days=1))
        with self.assertRaises(PromoError):
            services.validate_code(promo, subtotal=Decimal("65.00"), currency="USD", now=self.now)

    def test_at_start_boundary_passes(self):
        # now == starts_at: `now < starts_at` is False, so it's active.
        promo = make_promo(self.org, value="10", starts_at=self.now)
        self.assertIsNone(
            services.validate_code(promo, subtotal=Decimal("65.00"), currency="USD", now=self.now)
        )

    def test_after_end_window_rejected(self):
        promo = make_promo(self.org, value="10", ends_at=self.now - timedelta(days=1))
        with self.assertRaises(PromoError):
            services.validate_code(promo, subtotal=Decimal("65.00"), currency="USD", now=self.now)

    def test_within_window_passes(self):
        promo = make_promo(
            self.org,
            value="10",
            starts_at=self.now - timedelta(days=1),
            ends_at=self.now + timedelta(days=1),
        )
        self.assertIsNone(
            services.validate_code(promo, subtotal=Decimal("65.00"), currency="USD", now=self.now)
        )

    def test_maxed_out_rejected(self):
        promo = make_promo(self.org, value="10", max_redemptions=5)
        promo.redemption_count = 5
        promo.save(update_fields=["redemption_count"])
        with self.assertRaises(PromoError):
            services.validate_code(promo, subtotal=Decimal("65.00"), currency="USD", now=self.now)

    def test_below_cap_passes(self):
        promo = make_promo(self.org, value="10", max_redemptions=5)
        promo.redemption_count = 4
        promo.save(update_fields=["redemption_count"])
        self.assertIsNone(
            services.validate_code(promo, subtotal=Decimal("65.00"), currency="USD", now=self.now)
        )

    def test_below_min_order_amount_rejected(self):
        promo = make_promo(self.org, value="10", min_order_amount=Decimal("50.00"))
        with self.assertRaises(PromoError):
            services.validate_code(promo, subtotal=Decimal("49.99"), currency="USD", now=self.now)

    def test_at_min_order_amount_passes(self):
        # subtotal exactly == min_order_amount is allowed (`subtotal < min` False).
        promo = make_promo(self.org, value="10", min_order_amount=Decimal("50.00"))
        self.assertIsNone(
            services.validate_code(promo, subtotal=Decimal("50.00"), currency="USD", now=self.now)
        )

    def test_fixed_currency_mismatch_rejected(self):
        promo = make_promo(self.org, kind=PromoCode.Kind.FIXED, value="10", currency="EUR")
        with self.assertRaises(PromoError):
            services.validate_code(promo, subtotal=Decimal("65.00"), currency="USD", now=self.now)

    def test_fixed_currency_match_passes(self):
        promo = make_promo(self.org, kind=PromoCode.Kind.FIXED, value="10", currency="USD")
        self.assertIsNone(
            services.validate_code(promo, subtotal=Decimal("65.00"), currency="USD", now=self.now)
        )

    def test_fixed_blank_currency_not_checked(self):
        # A blank promo.currency means "the org's currency"; the caller has
        # already resolved the charge currency to that, so it isn't re-checked.
        promo = make_promo(self.org, kind=PromoCode.Kind.FIXED, value="10", currency="")
        self.assertIsNone(
            services.validate_code(promo, subtotal=Decimal("65.00"), currency="USD", now=self.now)
        )

    def test_discount_that_zeroes_cart_rejected(self):
        # A fixed code equal to the cart -> compute_discount == subtotal ->
        # rejected (Stripe won't create a $0 Checkout; comps are a separate flow).
        promo = make_promo(self.org, kind=PromoCode.Kind.FIXED, value="65")
        with self.assertRaises(PromoError):
            services.validate_code(promo, subtotal=Decimal("65.00"), currency="USD", now=self.now)

    def test_percent_over_100_that_exceeds_cart_rejected(self):
        promo = make_promo(self.org, kind=PromoCode.Kind.PERCENT, value="100")
        with self.assertRaises(PromoError):
            services.validate_code(promo, subtotal=Decimal("65.00"), currency="USD", now=self.now)

    def test_now_defaults_to_timezone_now_when_omitted(self):
        # Omitting `now` uses timezone.now(); an already-expired code still fails.
        promo = make_promo(self.org, value="10", ends_at=timezone.now() - timedelta(minutes=1))
        with self.assertRaises(PromoError):
            services.validate_code(promo, subtotal=Decimal("65.00"), currency="USD")


class GetUsableCodeTests(TestCase):
    def setUp(self):
        self.org = make_org("roxy")

    def test_case_insensitive_lookup(self):
        promo = make_promo(self.org, code="SUMMER10")
        found = services.get_usable_code(self.org, "summer10")
        self.assertEqual(found, promo)

    def test_whitespace_is_stripped_on_lookup(self):
        promo = make_promo(self.org, code="SUMMER10")
        self.assertEqual(services.get_usable_code(self.org, "  Summer10 "), promo)

    def test_unknown_code_returns_none(self):
        make_promo(self.org, code="SUMMER10")
        self.assertIsNone(services.get_usable_code(self.org, "NOPE"))

    def test_blank_code_returns_none(self):
        self.assertIsNone(services.get_usable_code(self.org, ""))
        self.assertIsNone(services.get_usable_code(self.org, "   "))
        self.assertIsNone(services.get_usable_code(self.org, None))

    def test_tenant_isolation_never_returns_another_orgs_code(self):
        org_b = make_org("other")
        make_promo(org_b, code="SUMMER10")  # org B's identical code text.
        # org A has no such code -> must NOT find org B's.
        self.assertIsNone(services.get_usable_code(self.org, "summer10"))

    def test_same_code_text_resolves_per_org(self):
        promo_a = make_promo(self.org, code="SUMMER10")
        org_b = make_org("other")
        promo_b = make_promo(org_b, code="SUMMER10")
        self.assertEqual(services.get_usable_code(self.org, "summer10"), promo_a)
        self.assertEqual(services.get_usable_code(org_b, "summer10"), promo_b)


class PromoCodeNormalizationTests(TestCase):
    def setUp(self):
        self.org = make_org("roxy")

    def test_save_normalizes_to_stripped_upper(self):
        promo = PromoCode.objects.create(
            organization=self.org, code="  Summer10 ", kind=PromoCode.Kind.PERCENT, value=Decimal("10")
        )
        promo.refresh_from_db()
        self.assertEqual(promo.code, "SUMMER10")

    def test_case_insensitive_uniqueness_within_org(self):
        make_promo(self.org, code="Summer10")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                make_promo(self.org, code="SUMMER10")

    def test_same_code_allowed_in_different_orgs(self):
        make_promo(self.org, code="Summer10")
        org_b = make_org("other")
        # Different org, identical (normalized) code text -> no collision.
        promo_b = make_promo(org_b, code="SUMMER10")
        self.assertEqual(promo_b.code, "SUMMER10")


class RecordRedemptionTests(TestCase):
    def setUp(self):
        self.org = make_org("roxy")

    def test_increments_by_one(self):
        promo = make_promo(self.org, value="10")
        self.assertEqual(promo.redemption_count, 0)
        services.record_redemption(promo)
        promo.refresh_from_db()
        self.assertEqual(promo.redemption_count, 1)

    def test_increments_are_cumulative(self):
        promo = make_promo(self.org, value="10")
        services.record_redemption(promo)
        services.record_redemption(promo)
        promo.refresh_from_db()
        self.assertEqual(promo.redemption_count, 2)
