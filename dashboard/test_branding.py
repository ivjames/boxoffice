"""Tests for the dashboard Branding area (dashboard.views.branding /
branding_derive): manager+ role gating, applying presets and custom schemes,
saving/deleting a tenant's own schemes, tenant isolation, and the
derive-from-homepage action (with the network agent stubbed). Setup style
mirrors dashboard/test_donations.py.

The non-view layer (model apply, extraction agent, preset seeding) is covered
in tenants/test_color_schemes.py.
"""

from unittest.mock import patch

from django.test import TestCase

from accounts.models import Membership
from accounts.tests import StaffFixtureMixin, host_for
from dashboard.tests import DashFixtureMixin
from tenants.models import ColorScheme

BRANDING_URL = "/dashboard/branding/"
DERIVE_URL = "/dashboard/branding/derive/"


class BrandingAccessTests(StaffFixtureMixin, DashFixtureMixin, TestCase):
    def setUp(self):
        self.org, self.venue = self.build_org("roxy")
        self.owner = self.make_staff(self.org, Membership.Role.OWNER)[0]
        self.manager = self.make_staff(self.org, Membership.Role.MANAGER)[0]
        self.box_office = self.make_staff(self.org, Membership.Role.BOX_OFFICE)[0]
        self.scanner = self.make_staff(self.org, Membership.Role.SCANNER)[0]

    def test_manager_and_above_only(self):
        for user, expected in [
            (self.owner, 200),
            (self.manager, 200),
            (self.box_office, 403),
            (self.scanner, 403),
        ]:
            self.client.logout()
            self.client.force_login(user)
            resp = self.client.get(BRANDING_URL, HTTP_HOST=host_for("roxy"))
            self.assertEqual(resp.status_code, expected, user.email)

    def test_anonymous_redirected_to_login(self):
        resp = self.client.get(BRANDING_URL, HTTP_HOST=host_for("roxy"))
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login/", resp.headers["Location"])

    def test_page_lists_presets(self):
        self.client.force_login(self.manager)
        resp = self.client.get(BRANDING_URL, HTTP_HOST=host_for("roxy"))
        self.assertContains(resp, "Ruby Velvet")
        self.assertContains(resp, "Ready-made schemes")

    def test_preset_cards_carry_preview_data(self):
        # Each preset exposes a client-side "Preview" button carrying its six
        # role colors, so the JS can load it into the customizer.
        self.client.force_login(self.manager)
        resp = self.client.get(BRANDING_URL, HTTP_HOST=host_for("roxy"))
        self.assertContains(resp, "scheme-preview-btn")
        self.assertContains(resp, 'data-feature_accent="#517D78"')  # Art Deco Royal's feature accent


class ApplySchemeViewTests(StaffFixtureMixin, DashFixtureMixin, TestCase):
    def setUp(self):
        self.org, self.venue = self.build_org("roxy")
        self.other_org, _ = self.build_org("other")
        self.manager = self.make_staff(self.org, Membership.Role.MANAGER)[0]
        self.client.force_login(self.manager)

    def _post(self, **data):
        return self.client.post(BRANDING_URL, data, HTTP_HOST=host_for("roxy"))

    def test_apply_preset_copies_colors_onto_org(self):
        preset = ColorScheme.objects.get(slug="art-deco-royal")
        resp = self._post(action="apply_scheme", scheme_id=preset.pk)
        self.assertRedirects(resp, BRANDING_URL, fetch_redirect_response=False)
        self.org.refresh_from_db()
        self.assertEqual(self.org.primary_color, "#4B2E83")
        self.assertEqual(self.org.accent_color, "#517D78")  # feature_accent role -> accent_color

    def test_cannot_apply_another_tenants_custom_scheme(self):
        foreign = ColorScheme.objects.create(
            organization=self.other_org, name="Foreign",
            primary="#010101", secondary="#020202", dark_accent="#030303",
            feature_accent="#040404", light_neutral="#fefefe", neutral="#050505",
        )
        resp = self._post(action="apply_scheme", scheme_id=foreign.pk)
        self.assertEqual(resp.status_code, 404)
        self.org.refresh_from_db()
        self.assertNotEqual(self.org.primary_color, "#010101")

    def test_save_custom_scheme(self):
        resp = self._post(
            action="save_scheme", name="House Red",
            primary="#7a0000", secondary="#a52222", dark_accent="#2a0000",
            feature_accent="#d4af37", light_neutral="#f5eaea", neutral="#120000",
        )
        self.assertRedirects(resp, BRANDING_URL, fetch_redirect_response=False)
        scheme = ColorScheme.objects.get(organization=self.org, name="House Red")
        self.assertFalse(scheme.is_preset)
        self.assertEqual(scheme.slug, "house-red")

    def test_save_and_apply_in_one_post(self):
        self._post(
            action="save_scheme", name="House Blue", apply_after_save="1",
            primary="#0a2a6b", secondary="#3b6ba5", dark_accent="#04122e",
            feature_accent="#c9a227", light_neutral="#eef3fa", neutral="#050b1c",
        )
        self.org.refresh_from_db()
        self.assertEqual(self.org.primary_color, "#0a2a6b")

    def test_duplicate_scheme_name_is_a_form_error(self):
        ColorScheme.objects.create(
            organization=self.org, name="Dupe",
            primary="#111111", secondary="#222222", dark_accent="#000000",
            feature_accent="#d4af37", light_neutral="#eeeeee", neutral="#101010",
        )
        resp = self._post(
            action="save_scheme", name="Dupe",
            primary="#111111", secondary="#222222", dark_accent="#000000",
            feature_accent="#d4af37", light_neutral="#eeeeee", neutral="#101010",
        )
        self.assertEqual(resp.status_code, 200)  # re-rendered, not redirected
        self.assertContains(resp, "already have a saved scheme")
        self.assertEqual(ColorScheme.objects.filter(organization=self.org, name="Dupe").count(), 1)

    def test_delete_own_custom_scheme(self):
        scheme = ColorScheme.objects.create(
            organization=self.org, name="Temp",
            primary="#111111", secondary="#222222", dark_accent="#000000",
            feature_accent="#d4af37", light_neutral="#eeeeee", neutral="#101010",
        )
        resp = self._post(action="delete_scheme", scheme_id=scheme.pk)
        self.assertRedirects(resp, BRANDING_URL, fetch_redirect_response=False)
        self.assertFalse(ColorScheme.objects.filter(pk=scheme.pk).exists())

    def test_cannot_delete_a_preset(self):
        preset = ColorScheme.objects.get(slug="art-deco-royal")
        resp = self._post(action="delete_scheme", scheme_id=preset.pk)
        self.assertEqual(resp.status_code, 404)
        self.assertTrue(ColorScheme.objects.filter(pk=preset.pk).exists())

    def _save_colors_data(self, **overrides):
        data = {
            "action": "save_colors",
            "primary_color": "#123456", "secondary_color": "#234567",
            "dark_accent_color": "#010101", "accent_color": "#d4af37",
            "light_neutral_color": "#fafafa", "neutral_color": "#020202",
            "heading_font": "system-sans", "body_font": "system-sans",
        }
        data.update(overrides)
        return data

    def test_save_colors_form(self):
        resp = self._post(**self._save_colors_data())
        self.assertRedirects(resp, BRANDING_URL, fetch_redirect_response=False)
        self.org.refresh_from_db()
        self.assertEqual(self.org.primary_color, "#123456")

    def test_save_fonts(self):
        resp = self._post(**self._save_colors_data(heading_font="playfair", body_font="lora"))
        self.assertRedirects(resp, BRANDING_URL, fetch_redirect_response=False)
        self.org.refresh_from_db()
        self.assertEqual(self.org.heading_font, "playfair")
        self.assertEqual(self.org.body_font, "lora")

    def test_invalid_font_is_rejected(self):
        resp = self._post(**self._save_colors_data(heading_font="comic-sans-lol"))
        self.assertEqual(resp.status_code, 200)  # re-rendered with errors
        self.org.refresh_from_db()
        self.assertEqual(self.org.heading_font, "system-sans")  # unchanged


class DeriveViewTests(StaffFixtureMixin, DashFixtureMixin, TestCase):
    def setUp(self):
        self.org, self.venue = self.build_org("roxy")
        self.manager = self.make_staff(self.org, Membership.Role.MANAGER)[0]
        self.client.force_login(self.manager)

    def test_derive_renders_suggested_palette(self):
        fake = {
            "name": "Roxy palette",
            "roles": {
                "primary": "#4b2e83", "secondary": "#7e5ba7", "dark_accent": "#0e0e12",
                "feature_accent": "#d4af37", "light_neutral": "#f2e8d6", "neutral": "#0e0e12",
            },
            "source_url": "https://roxy.example",
            "candidates": [("#4b2e83", 4), ("#d4af37", 2)],
        }
        with patch("dashboard.views.derive_scheme_from_url", return_value=fake) as m:
            resp = self.client.post(DERIVE_URL, {"url": "roxy.example"}, HTTP_HOST=host_for("roxy"))
        m.assert_called_once_with("roxy.example")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Suggested scheme")
        self.assertContains(resp, "Roxy palette")

    def test_derive_error_redirects_with_message(self):
        from tenants.color_extraction import ColorDeriveError

        with patch("dashboard.views.derive_scheme_from_url", side_effect=ColorDeriveError("nope")):
            resp = self.client.post(DERIVE_URL, {"url": "bad"}, HTTP_HOST=host_for("roxy"))
        self.assertRedirects(resp, BRANDING_URL, fetch_redirect_response=False)

    def test_derive_requires_a_url(self):
        resp = self.client.post(DERIVE_URL, {"url": ""}, HTTP_HOST=host_for("roxy"))
        self.assertRedirects(resp, BRANDING_URL, fetch_redirect_response=False)

    def test_derive_is_manager_gated(self):
        box_office = self.make_staff(self.org, Membership.Role.BOX_OFFICE)[0]
        self.client.logout()
        self.client.force_login(box_office)
        resp = self.client.post(DERIVE_URL, {"url": "roxy.example"}, HTTP_HOST=host_for("roxy"))
        self.assertEqual(resp.status_code, 403)
