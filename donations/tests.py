"""Service-layer tests for the donations app (Phase 2 order-foundation +
donations work): DonationCampaign.suggested_amount_list's lenient CSV parsing,
and services.get_or_create_general_fund's lazy, idempotent, tenant-scoped
resolution of the org's single v1 campaign. The money path that CONSUMES a
campaign (fulfill_donation, the donation OrderItem) is exercised from
payments/test_services.py; here we prove the campaign app in isolation, which
stays money-path-agnostic (it must not import orders/payments).
"""

from decimal import Decimal

from django.test import TestCase

from donations import services
from donations.models import DonationCampaign
from venues.tests import make_org


class SuggestedAmountListTests(TestCase):
    """suggested_amount_list() parses the presentation-only CSV of quick-pick
    give amounts leniently: a blank / non-numeric / non-positive chunk is
    skipped rather than raising, so a fat-fingered field can't 500 the
    storefront."""

    def setUp(self):
        self.org = make_org("roxy")

    def _campaign(self, suggested):
        return DonationCampaign.objects.create(organization=self.org, suggested_amounts=suggested)

    def test_clean_csv_parses_to_decimals_in_order(self):
        campaign = self._campaign("10,25,50,100")
        self.assertEqual(
            campaign.suggested_amount_list(),
            [Decimal("10"), Decimal("25"), Decimal("50"), Decimal("100")],
        )

    def test_default_value_parses(self):
        # The field's own model default, exercised through a freshly-created
        # campaign that didn't override it.
        campaign = DonationCampaign.objects.create(organization=self.org)
        self.assertEqual(
            campaign.suggested_amount_list(),
            [Decimal("10"), Decimal("25"), Decimal("50"), Decimal("100")],
        )

    def test_blanks_junk_and_negatives_are_dropped(self):
        # The docstring's own worked example: "10,,abc,-5,25" -> [10, 25].
        campaign = self._campaign("10,,abc,-5,25")
        self.assertEqual(campaign.suggested_amount_list(), [Decimal("10"), Decimal("25")])

    def test_whitespace_around_chunks_is_stripped(self):
        campaign = self._campaign(" 10 , 25 ")
        self.assertEqual(campaign.suggested_amount_list(), [Decimal("10"), Decimal("25")])

    def test_zero_is_dropped_as_non_positive(self):
        campaign = self._campaign("0,5")
        self.assertEqual(campaign.suggested_amount_list(), [Decimal("5")])

    def test_decimal_amounts_are_preserved(self):
        campaign = self._campaign("12.50,7.99")
        self.assertEqual(campaign.suggested_amount_list(), [Decimal("12.50"), Decimal("7.99")])

    def test_empty_string_yields_empty_list(self):
        campaign = self._campaign("")
        self.assertEqual(campaign.suggested_amount_list(), [])

    def test_all_junk_yields_empty_list(self):
        campaign = self._campaign(",,abc,-1,0,")
        self.assertEqual(campaign.suggested_amount_list(), [])


class GetOrCreateGeneralFundTests(TestCase):
    """get_or_create_general_fund lazily creates one active "General Fund" per
    org on first call and reuses it thereafter (idempotent), and is scoped per
    tenant -- two orgs get two distinct campaigns, and a campaign query for one
    org never sees another's."""

    def setUp(self):
        self.org_a = make_org("org-a")
        self.org_b = make_org("org-b")

    def test_creates_an_active_general_fund_on_first_call(self):
        self.assertEqual(DonationCampaign.objects.filter(organization=self.org_a).count(), 0)

        campaign = services.get_or_create_general_fund(self.org_a)

        self.assertEqual(campaign.organization_id, self.org_a.id)
        self.assertEqual(campaign.name, "General Fund")
        self.assertTrue(campaign.is_active)
        self.assertEqual(DonationCampaign.objects.filter(organization=self.org_a).count(), 1)

    def test_is_idempotent_reuses_the_same_campaign(self):
        first = services.get_or_create_general_fund(self.org_a)
        second = services.get_or_create_general_fund(self.org_a)

        self.assertEqual(first.pk, second.pk)
        self.assertEqual(DonationCampaign.objects.filter(organization=self.org_a).count(), 1)

    def test_resolves_to_the_oldest_campaign_when_several_exist(self):
        """The v1 one-campaign stance orders by (created_at, pk) so a later
        multi-campaign world keeps resolving to the original general fund
        rather than picking arbitrarily."""
        original = services.get_or_create_general_fund(self.org_a)
        # A hypothetical second appeal added later.
        DonationCampaign.objects.create(organization=self.org_a, name="Building Fund")

        resolved = services.get_or_create_general_fund(self.org_a)

        self.assertEqual(resolved.pk, original.pk)

    def test_two_orgs_get_distinct_campaigns(self):
        campaign_a = services.get_or_create_general_fund(self.org_a)
        campaign_b = services.get_or_create_general_fund(self.org_b)

        self.assertNotEqual(campaign_a.pk, campaign_b.pk)
        self.assertEqual(campaign_a.organization_id, self.org_a.id)
        self.assertEqual(campaign_b.organization_id, self.org_b.id)

    def test_creating_for_one_org_does_not_create_for_the_other(self):
        services.get_or_create_general_fund(self.org_a)
        self.assertEqual(DonationCampaign.objects.filter(organization=self.org_b).count(), 0)

    def test_campaign_queries_are_tenant_isolated(self):
        services.get_or_create_general_fund(self.org_a)
        services.get_or_create_general_fund(self.org_b)

        a_campaigns = DonationCampaign.objects.filter(organization=self.org_a)
        self.assertEqual(a_campaigns.count(), 1)
        self.assertTrue(all(c.organization_id == self.org_a.id for c in a_campaigns))


class DonationCampaignModelTests(TestCase):
    def setUp(self):
        self.org = make_org("roxy")

    def test_defaults(self):
        campaign = DonationCampaign.objects.create(organization=self.org)
        self.assertEqual(campaign.name, "General Fund")
        self.assertTrue(campaign.is_active)
        self.assertEqual(campaign.acknowledgment, "")

    def test_str_includes_name_and_org(self):
        campaign = DonationCampaign.objects.create(organization=self.org, name="Building Fund")
        self.assertIn("Building Fund", str(campaign))
        self.assertIn(str(self.org), str(campaign))
