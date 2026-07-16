"""Tests for the color-scheme feature's non-view layer: the six-role model
(ColorScheme + Organization.palette / apply_color_scheme), the built-in preset
seeding, and the derive-from-homepage extraction agent
(tenants.color_extraction). View/permission coverage lives in
dashboard/test_branding.py.
"""

from django.core.exceptions import ValidationError
from django.test import TestCase

from tenants.color_extraction import (
    ColorDeriveError,
    _guard_public_url,
    assign_roles,
    derive_scheme_from_url,
    extract_candidate_colors,
)
from tenants.color_schemes import BUILTIN_SCHEMES, ROLE_KEYS, ROLE_TO_ORG_FIELD
from tenants.models import ColorScheme, Organization


def make_org(subdomain="roxy"):
    return Organization.objects.create(
        name=subdomain.title(), slug=subdomain, subdomain=subdomain,
        contact_email=f"a@{subdomain}.example",
    )


class PresetSeedingTests(TestCase):
    def test_all_builtin_presets_are_seeded(self):
        # The preset seed/re-sync migrations ran; every BUILTIN_SCHEMES entry is
        # a preset row (organization NULL, is_preset True) with a full palette.
        for slug, name, roles in BUILTIN_SCHEMES:
            scheme = ColorScheme.objects.get(organization=None, slug=slug)
            self.assertTrue(scheme.is_preset)
            self.assertEqual(scheme.name, name)
            self.assertEqual(scheme.roles, roles)

    def test_seed_command_is_idempotent(self):
        from django.core.management import call_command

        before = ColorScheme.objects.filter(is_preset=True).count()
        call_command("seed_color_schemes")
        self.assertEqual(ColorScheme.objects.filter(is_preset=True).count(), before)


class ApplySchemeTests(TestCase):
    def test_palette_reads_off_the_org_fields(self):
        org = make_org()
        self.assertEqual(org.palette["feature_accent"], org.accent_color)
        self.assertEqual(set(org.palette), set(ROLE_KEYS))

    def test_apply_copies_every_role_onto_the_org(self):
        org = make_org()
        scheme = ColorScheme.objects.get(slug="art-deco-royal")
        org.apply_color_scheme(scheme)
        org.refresh_from_db()
        for role, field in ROLE_TO_ORG_FIELD.items():
            self.assertEqual(getattr(org, field), scheme.roles[role])
        # 'feature_accent' lands on the legacy accent_color field specifically.
        self.assertEqual(org.accent_color, scheme.feature_accent)

    def test_apply_is_a_snapshot_not_a_live_link(self):
        # Editing the source scheme after applying must NOT re-theme the org.
        org = make_org()
        scheme = ColorScheme.objects.get(slug="art-deco-royal")
        org.apply_color_scheme(scheme)
        scheme.primary = "#000000"
        scheme.save()
        org.refresh_from_db()
        self.assertEqual(org.primary_color, "#4B2E83")


class ColorSchemeModelTests(TestCase):
    def test_slug_autofills_from_name(self):
        org = make_org()
        scheme = ColorScheme.objects.create(
            organization=org, name="Sunset Boulevard",
            primary="#111111", secondary="#222222", dark_accent="#000000",
            feature_accent="#d4af37", light_neutral="#eeeeee", neutral="#101010",
        )
        self.assertEqual(scheme.slug, "sunset-boulevard")

    def test_hex_validation_rejects_garbage(self):
        org = make_org()
        scheme = ColorScheme(
            organization=org, name="Bad", primary="not-a-color",
            secondary="#222222", dark_accent="#000000", feature_accent="#d4af37",
            light_neutral="#eeeeee", neutral="#101010",
        )
        with self.assertRaises(ValidationError):
            scheme.full_clean()


class FontTests(TestCase):
    def test_font_stack_and_google_families(self):
        from tenants.fonts import font_stack, google_families

        self.assertIn("Playfair Display", font_stack("playfair"))
        # Unknown key falls back to a real stack, never empty.
        self.assertTrue(font_stack("nope"))
        # System stacks contribute no Google family; web fonts do, de-duped.
        self.assertEqual(google_families("system-sans"), [])
        self.assertEqual(google_families("playfair", "playfair"), ["Playfair+Display:wght@400;600;700"])

    def test_org_font_properties(self):
        org = make_org()
        org.heading_font = "playfair"
        org.body_font = "lora"
        self.assertIn("Playfair Display", org.heading_font_stack)
        self.assertIn("Lora", org.body_font_stack)
        self.assertEqual(len(org.google_font_families), 2)

    def test_defaults_are_system_stacks_with_no_google_load(self):
        org = make_org()
        self.assertEqual(org.google_font_families, [])


class ExtractionTests(TestCase):
    SAMPLE_HTML = """
    <html><head><style>
      body { background:#F2E8D6; color:#0E0E12; }
      .brand { background:#4B2E83; }
      .brand2 { background:#4B2E83; }
      a { color:#D4AF37; }
      .sec { background: rgb(126, 91, 167); }
    </style><link rel="stylesheet" href="/theme.css"></head><body></body></html>
    """

    def _fetch(self, url):
        if url.endswith("theme.css"):
            return ".cta{background:#4B2E83}.gold{color:#d4af37}"
        return self.SAMPLE_HTML

    def test_candidates_are_weighted_by_frequency(self):
        cands = extract_candidate_colors(
            self.SAMPLE_HTML, base_url="https://roxy.example/", fetch=self._fetch
        )
        as_dict = dict(cands)
        # #4b2e83 appears twice inline + once in theme.css == the top color.
        self.assertEqual(cands[0][0], "#4b2e83")
        self.assertGreaterEqual(as_dict["#4b2e83"], 3)
        # rgb() and named forms are normalized to #rrggbb.
        self.assertIn("#7e5ba7", as_dict)

    def test_stylesheet_fetch_failure_is_not_fatal(self):
        def flaky(url):
            if url.endswith("theme.css"):
                raise RuntimeError("boom")
            return self.SAMPLE_HTML

        cands = extract_candidate_colors(
            self.SAMPLE_HTML, base_url="https://roxy.example/", fetch=flaky
        )
        self.assertTrue(any(c == "#4b2e83" for c, _w in cands))

    def test_assign_roles_maps_by_luminance_and_warmth(self):
        cands = extract_candidate_colors(
            self.SAMPLE_HTML, base_url="https://roxy.example/", fetch=self._fetch
        )
        roles = assign_roles(cands)
        self.assertEqual(roles["primary"], "#4b2e83")
        self.assertEqual(roles["feature_accent"], "#d4af37")  # warmest saturated
        self.assertEqual(roles["dark_accent"], "#0e0e12")  # darkest
        self.assertEqual(set(roles), set(ROLE_KEYS))

    def test_assign_roles_falls_back_on_empty(self):
        roles = assign_roles([])
        self.assertEqual(set(roles), set(ROLE_KEYS))
        self.assertTrue(all(v.startswith("#") for v in roles.values()))

    def test_derive_returns_complete_scheme(self):
        derived = derive_scheme_from_url("roxy.example", fetch=self._fetch)
        self.assertEqual(set(derived["roles"]), set(ROLE_KEYS))
        self.assertEqual(derived["source_url"], "https://roxy.example")
        self.assertEqual(derived["roles"]["primary"], "#4b2e83")
        self.assertTrue(derived["name"])

    def test_derive_wraps_fetch_errors(self):
        def dead(url):
            raise ConnectionError("no route")

        with self.assertRaises(ColorDeriveError):
            derive_scheme_from_url("roxy.example", fetch=dead)


class SSRFGuardTests(TestCase):
    """The derive fetch must reject non-public hosts (SSRF hardening) -- IP
    literals + localhost resolve without network, so these don't hit DNS."""

    def test_rejects_loopback_private_and_link_local(self):
        for url in (
            "http://localhost/",
            "http://127.0.0.1/",
            "http://10.0.0.5/",
            "http://192.168.1.1/",
            "http://169.254.169.254/latest/meta-data/",  # cloud metadata
        ):
            with self.assertRaises(ColorDeriveError, msg=url):
                _guard_public_url(url)

    def test_rejects_non_http_schemes(self):
        for url in ("ftp://example.com/", "file:///etc/passwd", "gopher://x/"):
            with self.assertRaises(ColorDeriveError, msg=url):
                _guard_public_url(url)

    def test_allows_a_public_ip(self):
        _guard_public_url("https://8.8.8.8/")  # must not raise
