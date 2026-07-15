"""Phase 4 marketing-consent + unsubscribe-token tests for the guests app.

Covers the two consent setters (guests.services.record_marketing_opt_in /
set_marketing_opt_in) and their invariants -- record is ONE-WAY and never
re-stamps; set is two-way and retains the audit timestamp on opt-out -- plus the
signed one-click unsubscribe token round-trip (guests.tokens) and
GuestAccount.tag_list parsing.

(audience_queryset -- also a Phase 4 service fn -- is exercised in
campaigns/tests.py, co-located with its module campaigns.services.)
"""

import time
from unittest import mock

from django.test import TestCase

from venues.tests import make_org

from . import services
from .models import GuestAccount
from .tokens import make_unsubscribe_token, read_unsubscribe_token


class MarketingConsentFixtureMixin:
    def setUp(self):
        self.org = make_org("roxy")

    def guest(self, email="buyer@example.com", *, opt_in=False, at=None):
        return GuestAccount.objects.create(
            organization=self.org, email=email, marketing_opt_in=opt_in, marketing_opt_in_at=at
        )


class RecordMarketingOptInTests(MarketingConsentFixtureMixin, TestCase):
    """record_marketing_opt_in: the checkout path -- one-way, idempotent, never
    re-stamps, never opts anyone out."""

    def test_fresh_guest_opts_in_and_stamps_timestamp(self):
        g = self.guest(opt_in=False)
        services.record_marketing_opt_in(g)
        g.refresh_from_db()
        self.assertTrue(g.marketing_opt_in)
        self.assertIsNotNone(g.marketing_opt_in_at)

    def test_already_opted_in_guest_keeps_original_timestamp(self):
        g = self.guest(opt_in=False)
        services.record_marketing_opt_in(g)
        g.refresh_from_db()
        original = g.marketing_opt_in_at

        services.record_marketing_opt_in(g)  # a later purchase, box ticked again
        g.refresh_from_db()
        self.assertTrue(g.marketing_opt_in)
        self.assertEqual(g.marketing_opt_in_at, original)  # never re-stamped

    def test_none_guest_is_a_no_op(self):
        # A Stripe session may carry no email -> guest=None; must not raise.
        services.record_marketing_opt_in(None)

    def test_never_opts_a_subscribed_guest_back_out(self):
        g = self.guest(opt_in=False)
        services.record_marketing_opt_in(g)
        # A later fulfillment path calling record again (or with the box
        # un-ticked, in which case the caller simply wouldn't call it) can only
        # ever leave them opted IN -- record has no path to False.
        services.record_marketing_opt_in(g)
        g.refresh_from_db()
        self.assertTrue(g.marketing_opt_in)


class SetMarketingOptInTests(MarketingConsentFixtureMixin, TestCase):
    """set_marketing_opt_in: the deliberate two-way control (portal toggle /
    one-click unsubscribe)."""

    def test_opting_in_sets_flag_and_stamps(self):
        g = self.guest(opt_in=False)
        services.set_marketing_opt_in(g, True)
        g.refresh_from_db()
        self.assertTrue(g.marketing_opt_in)
        self.assertIsNotNone(g.marketing_opt_in_at)

    def test_reaffirming_opt_in_keeps_original_stamp(self):
        g = self.guest(opt_in=False)
        services.set_marketing_opt_in(g, True)
        g.refresh_from_db()
        original = g.marketing_opt_in_at

        services.set_marketing_opt_in(g, True)  # toggled on while already on
        g.refresh_from_db()
        self.assertEqual(g.marketing_opt_in_at, original)

    def test_opting_out_clears_flag_but_retains_timestamp_for_audit(self):
        g = self.guest(opt_in=False)
        services.set_marketing_opt_in(g, True)
        g.refresh_from_db()
        stamped = g.marketing_opt_in_at
        self.assertIsNotNone(stamped)

        services.set_marketing_opt_in(g, False)
        g.refresh_from_db()
        self.assertFalse(g.marketing_opt_in)
        # The "when they had consented" record is intentionally kept.
        self.assertEqual(g.marketing_opt_in_at, stamped)

    def test_none_guest_is_a_no_op(self):
        services.set_marketing_opt_in(None, True)
        services.set_marketing_opt_in(None, False)


class UnsubscribeTokenTests(MarketingConsentFixtureMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.g = self.guest()

    def test_make_read_round_trip_returns_guest_pk(self):
        token = make_unsubscribe_token(self.g)
        self.assertEqual(read_unsubscribe_token(token, self.org), self.g.pk)

    def test_tampered_token_returns_none(self):
        token = make_unsubscribe_token(self.g)
        tampered = token[:-1] + ("A" if token[-1] != "A" else "B")
        self.assertIsNone(read_unsubscribe_token(tampered, self.org))

    def test_cross_org_token_returns_none(self):
        org_b = make_org("otherhouse")
        token = make_unsubscribe_token(self.g)  # minted for self.org
        self.assertIsNone(read_unsubscribe_token(token, org_b))

    def test_no_default_expiry_keeps_an_old_token_valid(self):
        # Sign the token with an artificially old timestamp; the unsubscribe
        # reader defaults to max_age=None, so age never invalidates it -- an
        # explicit short max_age WOULD reject it, proving no-expiry is the
        # default behavior (not just that the token happens to be fresh).
        old = time.time() - 10_000_000  # ~116 days ago
        with mock.patch("time.time", return_value=old):
            token = make_unsubscribe_token(self.g)

        self.assertEqual(read_unsubscribe_token(token, self.org), self.g.pk)  # default None
        self.assertIsNone(read_unsubscribe_token(token, self.org, max_age=1))

    def test_replay_is_idempotent(self):
        token = make_unsubscribe_token(self.g)
        self.assertEqual(read_unsubscribe_token(token, self.org), self.g.pk)
        self.assertEqual(read_unsubscribe_token(token, self.org), self.g.pk)


class TagListTests(MarketingConsentFixtureMixin, TestCase):
    _tag_seq = 0

    def _tags(self, raw):
        # Unique email per call so repeated calls in one test don't collide on
        # the (org, email) unique constraint.
        type(self)._tag_seq += 1
        g = self.guest(email=f"tagged{self._tag_seq}@example.com")
        g.tags = raw
        g.save(update_fields=["tags"])
        return g.tag_list()

    def test_parses_strips_dedupes_and_preserves_order_dropping_blanks(self):
        # "vip, , subscriber,vip" -> ["vip", "subscriber"] (blank + exact dupe
        # dropped, first-occurrence order kept).
        self.assertEqual(self._tags("vip, , subscriber,vip"), ["vip", "subscriber"])

    def test_case_preserved_and_only_exact_repeats_dropped(self):
        # Case is preserved and "VIP" != "vip", so both survive.
        self.assertEqual(self._tags("vip, VIP, vip"), ["vip", "VIP"])

    def test_empty_tags_yield_empty_list(self):
        self.assertEqual(self._tags(""), [])
        self.assertEqual(self._tags("  ,  ,"), [])
