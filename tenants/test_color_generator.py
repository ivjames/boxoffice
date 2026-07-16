"""Tests for the WCAG color generator (tenants.color_generator): the contrast
math and the "best-of-two per surface" contract that produces the shipped
BUILTIN_SCHEMES from SOURCE_SCHEMES.
"""

from django.test import SimpleTestCase

from tenants.color_generator import (
    AA,
    _is_light,
    adjust_scheme,
    build_wcag_schemes,
    contrast_ratio,
    readable_on,
    relative_luminance,
    scheme_report,
    text_over,
)
from tenants.color_schemes import BUILTIN_SCHEMES, SOURCE_SCHEMES

# The surfaces text is placed over (brand fills + the page background).
_SURFACES = ("primary", "secondary", "feature_accent", "dark_accent", "light_neutral")


def _text_for(fill, roles):
    """The neutral used as text over `fill` under best-of-two: dark text
    (`neutral`) on a light fill, light text (`light_neutral`) on a dark fill."""
    return roles["neutral"] if _is_light(fill) else roles["light_neutral"]


class ContrastMathTests(SimpleTestCase):
    def test_luminance_endpoints(self):
        self.assertAlmostEqual(relative_luminance("#FFFFFF"), 1.0, places=4)
        self.assertAlmostEqual(relative_luminance("#000000"), 0.0, places=4)

    def test_contrast_black_on_white_is_21(self):
        self.assertAlmostEqual(contrast_ratio("#000000", "#FFFFFF"), 21.0, places=2)
        self.assertAlmostEqual(contrast_ratio("#FFFFFF", "#000000"), 21.0, places=2)

    def test_identical_colors_have_ratio_one(self):
        self.assertAlmostEqual(contrast_ratio("#4B2E83", "#4B2E83"), 1.0, places=4)

    def test_shorthand_hex_is_expanded(self):
        # The hex validator accepts #abc; contrast math must not crash on it.
        self.assertAlmostEqual(relative_luminance("#fff"), relative_luminance("#ffffff"), places=6)
        self.assertAlmostEqual(contrast_ratio("#000", "#fff"), 21.0, places=2)


class AdjustSchemeTests(SimpleTestCase):
    DARK = {  # dark primary
        "primary": "#4B2E83", "secondary": "#7E5BA7", "feature_accent": "#517D78",
        "dark_accent": "#2A132F", "light_neutral": "#F2E8D6", "neutral": "#0E0E12",
    }
    LIGHT = {  # light primary (Powder Blue) -- the case fixed roles couldn't do
        "primary": "#A9C3DD", "secondary": "#D2E1EF", "feature_accent": "#C9887E",
        "dark_accent": "#24384F", "light_neutral": "#FAFCFE", "neutral": "#26303A",
    }

    def test_only_neutrals_can_change(self):
        adjusted, _ = adjust_scheme(self.DARK)
        for role in ("primary", "secondary", "feature_accent", "dark_accent"):
            self.assertEqual(adjusted[role], self.DARK[role], role)

    def test_dark_primary_scheme_reaches_aa(self):
        adjusted, warnings = adjust_scheme(self.DARK)
        self.assertEqual(warnings, [])
        for fill in _SURFACES:
            self.assertGreaterEqual(
                contrast_ratio(_text_for(adjusted[fill], adjusted), adjusted[fill]), AA, fill
            )

    def test_light_primary_scheme_now_reaches_aa(self):
        # Best-of-two puts DARK text on the light primary -- reachable, no warning.
        adjusted, warnings = adjust_scheme(self.LIGHT)
        self.assertEqual(warnings, [])
        self.assertTrue(_is_light(adjusted["primary"]))
        self.assertGreaterEqual(contrast_ratio(adjusted["neutral"], adjusted["primary"]), AA)
        self.assertGreaterEqual(contrast_ratio(adjusted["light_neutral"], adjusted["dark_accent"]), AA)

    def test_generation_is_idempotent(self):
        once = build_wcag_schemes([("x", "X", self.DARK)])
        twice = build_wcag_schemes([("x", "X", once[0][2])])
        self.assertEqual(once[0][2], twice[0][2])


class TextOverTests(SimpleTestCase):
    def test_dark_fill_gets_light_text_and_vice_versa(self):
        ln, n = "#F7EFE3", "#181312"
        self.assertEqual(text_over("#2C0E17", ln, n), ln)  # dark fill -> light text
        self.assertEqual(text_over("#D89AA6", ln, n), n)   # light fill -> dark text

    def test_text_over_is_always_aa(self):
        # The chosen text clears AA over every shipped scheme's fills.
        ln_role, n_role = "light_neutral", "neutral"
        for _slug, name, roles in BUILTIN_SCHEMES:
            for fill_role in ("primary", "secondary", "feature_accent", "dark_accent"):
                fill = roles[fill_role]
                text = text_over(fill, roles[ln_role], roles[n_role])
                self.assertGreaterEqual(contrast_ratio(text, fill), AA, f"{name}.{fill_role}")


class ReadableOnTests(SimpleTestCase):
    def test_pale_color_is_darkened_to_pass(self):
        # A pale blush is illegible on a near-white page; ink darkens it to AA.
        bg = "#FFF8F4"
        self.assertLess(contrast_ratio("#D89AA6", bg), AA)
        ink = readable_on("#D89AA6", bg)
        self.assertGreaterEqual(contrast_ratio(ink, bg), AA)

    def test_already_legible_color_is_unchanged(self):
        bg = "#F2E8D6"
        self.assertEqual(readable_on("#4B2E83", bg), "#4B2E83")

    def test_ink_clears_aa_for_every_shipped_scheme(self):
        for _slug, name, roles in BUILTIN_SCHEMES:
            bg = roles["light_neutral"]
            for role in ("primary", "secondary", "feature_accent"):
                self.assertGreaterEqual(
                    contrast_ratio(readable_on(roles[role], bg), bg), AA, f"{name}.{role}"
                )


class ShippedCatalogTests(SimpleTestCase):
    def test_every_shipped_scheme_is_aa_compliant(self):
        # Under best-of-two, the chosen text neutral clears AA over every surface
        # of every shipped scheme.
        for slug, name, roles in BUILTIN_SCHEMES:
            for fill_role in _SURFACES:
                fill = roles[fill_role]
                text = _text_for(fill, roles)
                self.assertGreaterEqual(
                    contrast_ratio(text, fill), AA, f"{name}: text on {fill_role} {fill}"
                )

    def test_untouched_brand_roles_match_source(self):
        # primary / secondary / dark_accent pass through the generator unchanged;
        # only the feature accent (harmonized) and the two neutrals may move.
        source = {slug: roles for slug, _n, roles in SOURCE_SCHEMES}
        for slug, _name, roles in BUILTIN_SCHEMES:
            for role in ("primary", "secondary", "dark_accent"):
                self.assertEqual(roles[role], source[slug][role], f"{slug}.{role}")

    def test_feature_accent_is_harmonized(self):
        # The shipped accent is the harmonized (analogous) accent, and it stays
        # visibly distinct from the primary fill.
        from tenants.color_generator import (
            harmonize_accent,
            ACCENT_MIN_CONTRAST_VS_PRIMARY,
        )

        source = {slug: roles for slug, _n, roles in SOURCE_SCHEMES}
        for slug, _name, roles in BUILTIN_SCHEMES:
            expected = harmonize_accent(source[slug])["feature_accent"]
            self.assertEqual(roles["feature_accent"], expected, f"{slug}.feature_accent")
            self.assertGreaterEqual(
                contrast_ratio(roles["feature_accent"], roles["primary"]),
                ACCENT_MIN_CONTRAST_VS_PRIMARY - 0.05,  # rounding slack
                f"{slug}: accent vs primary too close",
            )

    def test_report_has_no_shortfalls(self):
        report = scheme_report(SOURCE_SCHEMES)
        self.assertEqual(len(report), len(SOURCE_SCHEMES))
        self.assertEqual([r for r in report if r["warnings"]], [])

    def test_harmonized_accents_avoid_the_muddy_band(self):
        # No shipped accent lands in the olive/khaki dead-zone the rule steers
        # around (the source's gold/yellow primaries used to produce olive).
        from tenants.color_generator import _hls, _MUDDY_ACCENT_BAND, harmonize_accent

        lo, hi = _MUDDY_ACCENT_BAND
        source = {slug: roles for slug, _n, roles in SOURCE_SCHEMES}
        for slug, _name, roles in BUILTIN_SCHEMES:
            if harmonize_accent(source[slug])["feature_accent"] == source[slug]["feature_accent"]:
                continue  # neutral scheme kept its curated accent -- not derived
            hue = _hls(roles["feature_accent"])[0]
            self.assertFalse(lo <= hue <= hi, f"{slug}: accent hue {hue:.3f} is in the muddy band")


class DarkThemeTests(SimpleTestCase):
    def test_dark_variant_is_dark_and_legible(self):
        # Every scheme's derived dark theme has a genuinely dark page, and its
        # text + brand ink all clear WCAG AA on that page.
        from tenants.color_generator import dark_surfaces, dark_ink, TEXT_LUMINANCE_THRESHOLD

        for slug, name, roles in BUILTIN_SCHEMES:
            d = dark_surfaces(roles)
            self.assertLess(
                relative_luminance(d["bg"]), TEXT_LUMINANCE_THRESHOLD,
                f"{name}: dark bg {d['bg']} is not dark",
            )
            self.assertGreaterEqual(
                contrast_ratio(d["text"], d["bg"]), AA, f"{name}: body text on dark bg",
            )
            for role in ("primary", "feature_accent", "secondary"):
                ink = dark_ink(roles[role], d["bg"])
                self.assertGreaterEqual(
                    contrast_ratio(ink, d["bg"]), AA, f"{name}: {role} ink on dark bg",
                )
