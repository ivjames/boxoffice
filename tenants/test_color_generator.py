"""Tests for the WCAG color generator (tenants.color_generator): the contrast
math and the "fixed roles" adjustment contract. The generator is not yet wired
into the shipped catalog, but the math is committed code, so it's covered here.
"""

from django.test import SimpleTestCase

from tenants.color_generator import (
    AA,
    adjust_scheme,
    build_wcag_schemes,
    contrast_ratio,
    relative_luminance,
    scheme_report,
)
from tenants.color_schemes import BUILTIN_SCHEMES


class ContrastMathTests(SimpleTestCase):
    def test_luminance_endpoints(self):
        self.assertAlmostEqual(relative_luminance("#FFFFFF"), 1.0, places=4)
        self.assertAlmostEqual(relative_luminance("#000000"), 0.0, places=4)

    def test_contrast_black_on_white_is_21(self):
        self.assertAlmostEqual(contrast_ratio("#000000", "#FFFFFF"), 21.0, places=2)
        # Symmetric.
        self.assertAlmostEqual(contrast_ratio("#FFFFFF", "#000000"), 21.0, places=2)

    def test_identical_colors_have_ratio_one(self):
        self.assertAlmostEqual(contrast_ratio("#4B2E83", "#4B2E83"), 1.0, places=4)


class AdjustSchemeTests(SimpleTestCase):
    # A dark-primary scheme (Art Deco Royal) -- fully fixable under fixed roles.
    DARK = {
        "primary": "#4B2E83", "secondary": "#7E5BA7", "feature_accent": "#517D78",
        "dark_accent": "#2A132F", "light_neutral": "#F2E8D6", "neutral": "#0E0E12",
    }
    # A light-primary scheme (Powder Blue) -- light text can't clear AA on primary.
    LIGHT = {
        "primary": "#A9C3DD", "secondary": "#D2E1EF", "feature_accent": "#C9887E",
        "dark_accent": "#24384F", "light_neutral": "#FAFCFE", "neutral": "#26303A",
    }

    def test_only_neutrals_can_change(self):
        adjusted, _warnings = adjust_scheme(self.DARK)
        for role in ("primary", "secondary", "feature_accent", "dark_accent"):
            self.assertEqual(adjusted[role], self.DARK[role], role)

    def test_dark_primary_scheme_reaches_aa(self):
        adjusted, warnings = adjust_scheme(self.DARK)
        self.assertEqual(warnings, [])
        # light_neutral (light text) clears AA over the dark fills.
        self.assertGreaterEqual(contrast_ratio(adjusted["light_neutral"], adjusted["primary"]), AA)
        self.assertGreaterEqual(contrast_ratio(adjusted["light_neutral"], adjusted["dark_accent"]), AA)
        # neutral (dark text) clears AA over light_neutral.
        self.assertGreaterEqual(contrast_ratio(adjusted["neutral"], adjusted["light_neutral"]), AA)

    def test_light_primary_scheme_is_flagged(self):
        adjusted, warnings = adjust_scheme(self.LIGHT)
        # Light text over a light primary can't reach AA -- capped and reported.
        self.assertTrue(warnings)
        self.assertLess(contrast_ratio(adjusted["light_neutral"], adjusted["primary"]), AA)

    def test_generation_is_idempotent(self):
        once = build_wcag_schemes([("x", "X", self.DARK)])
        twice = build_wcag_schemes([("x", "X", once[0][2])])
        self.assertEqual(once[0][2], twice[0][2])


class ReportTests(SimpleTestCase):
    def test_report_covers_every_scheme(self):
        report = scheme_report(BUILTIN_SCHEMES)
        self.assertEqual(len(report), len(BUILTIN_SCHEMES))
        # The known light-primary schemes surface AA warnings under fixed roles.
        by_slug = {r["slug"]: r for r in report}
        self.assertTrue(by_slug["powder-blue"]["warnings"])
