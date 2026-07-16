"""WCAG-aware color-map generator.

The 36 built-in schemes (tenants.color_schemes.BUILTIN_SCHEMES) carry the
design's exact brand colors. This module takes a source palette and produces a
WCAG-nudged one by shifting ONLY the two neutral/text roles so they clear
contrast against the surfaces they sit on -- the four brand colors (primary /
secondary / feature_accent / dark_accent) are never touched.

NOTE: this generator is not yet applied to the shipped catalog -- the contrast
contract below ("fixed roles") is still being decided, because it leaves 19 of
36 schemes below AA (every scheme with a light primary: light text can't clear
AA over a light fill). `manage.py generate_color_schemes` reports that. A
"best-of-two per surface" contract resolves all 36 and is the likely successor.

Contract (chosen for this palette -- "fixed roles"):

  * `neutral` is the dark body-text color, shown over the `light_neutral`
    page background. Darken it until it clears the target over light_neutral.
  * `light_neutral` is the light text color, shown over the dark brand fills
    (`primary`, `dark_accent`) and over `neutral`. Lighten it until it clears
    the target over all of those.

Only luminance moves -- the adjustment shifts a color's HSL lightness while
holding hue and saturation, so a "Champagne Cream" stays cream, just a shade
lighter/darker. Target is WCAG AA (4.5:1), upgraded to AAA (7:1) when reaching
it costs only a small extra nudge (AAA_CHEAP_DELTA_L).

Some schemes have a *light* primary (Powder Blue, Blush, Sea Glass...): light
text can't clear AA over a light fill no matter how light it goes, so
`light_neutral` caps at white and `build_wcag_schemes` records the shortfall.
`manage.py generate_color_schemes` reports those. The generation is pure and
idempotent: re-running over an already-compliant palette is a no-op.
"""

import colorsys

# WCAG 2.x contrast thresholds for text.
AA = 4.5
AAA = 7.0

# Upgrade an AA-passing color to AAA only when the extra HSL-lightness move
# beyond the AA solution is at most this much -- i.e. AAA is "cheap" here.
AAA_CHEAP_DELTA_L = 0.08

# How finely we scan lightness looking for the smallest move that clears the
# target. 1/256 steps is well below 8-bit color resolution.
_SCAN_STEPS = 256


# --- color math -----------------------------------------------------------


def _to_rgb(hex_color):
    h = hex_color.lstrip("#")
    return tuple(int(h[i : i + 2], 16) / 255 for i in (0, 2, 4))


def _to_hex(rgb):
    return "#%02X%02X%02X" % tuple(max(0, min(255, round(c * 255))) for c in rgb)


def _linearize(channel):
    return channel / 12.92 if channel <= 0.03928 else ((channel + 0.055) / 1.055) ** 2.4


def relative_luminance(hex_color):
    """WCAG relative luminance (0..1) of an sRGB hex color."""
    r, g, b = _to_rgb(hex_color)
    return 0.2126 * _linearize(r) + 0.7152 * _linearize(g) + 0.0722 * _linearize(b)


def contrast_ratio(hex_a, hex_b):
    """WCAG contrast ratio between two hex colors (1.0 .. 21.0)."""
    la, lb = relative_luminance(hex_a), relative_luminance(hex_b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def _lightness(hex_color):
    r, g, b = _to_rgb(hex_color)
    return colorsys.rgb_to_hls(r, g, b)[1]


def _with_lightness(hex_color, lightness):
    """The color with its HSL lightness replaced (hue + saturation held)."""
    r, g, b = _to_rgb(hex_color)
    h, _l, s = colorsys.rgb_to_hls(r, g, b)
    return _to_hex(colorsys.hls_to_rgb(h, min(1.0, max(0.0, lightness)), s))


def _min_contrast(hex_color, backgrounds):
    return min(contrast_ratio(hex_color, bg) for bg in backgrounds)


# --- adjustment -----------------------------------------------------------


def _search(hex_color, backgrounds, target, lighten):
    """The color closest to `hex_color` (moving lightness up if `lighten`, else
    down) whose contrast against every background is >= target, verified on the
    ROUNDED 8-bit hex we actually emit. Returns (hex, reached): `reached` is
    False when even the cap (white/black) can't hit the target, in which case
    the capped color is returned as the best effort."""
    if _min_contrast(hex_color, backgrounds) >= target:
        return hex_color, True
    origin = _lightness(hex_color)
    bound = 1.0 if lighten else 0.0
    for step in range(1, _SCAN_STEPS + 1):
        frac = step / _SCAN_STEPS
        candidate = _with_lightness(hex_color, origin + (bound - origin) * frac)
        if _min_contrast(candidate, backgrounds) >= target:
            return candidate, True
    return _with_lightness(hex_color, bound), False


def _adjust(hex_color, backgrounds, lighten):
    """Nudge `hex_color` to clear AA over `backgrounds`, upgrading to AAA when
    cheap. Returns (hex, meets_aa)."""
    aa_hex, aa_ok = _search(hex_color, backgrounds, AA, lighten)
    if not aa_ok:
        return aa_hex, False
    aaa_hex, aaa_ok = _search(hex_color, backgrounds, AAA, lighten)
    if aaa_ok and abs(_lightness(aaa_hex) - _lightness(aa_hex)) <= AAA_CHEAP_DELTA_L:
        return aaa_hex, True
    return aa_hex, True


def adjust_scheme(roles):
    """Return (adjusted_roles, warnings) for one scheme's role dict. Only
    `light_neutral` and `neutral` may change; brand roles pass through
    untouched. `warnings` lists human-readable AA shortfalls (light-primary
    schemes)."""
    out = dict(roles)
    warnings = []

    # light_neutral: light text over the dark fills + neutral. Lighten to clear.
    ln_backgrounds = [roles["primary"], roles["dark_accent"], roles["neutral"]]
    out["light_neutral"], ln_ok = _adjust(roles["light_neutral"], ln_backgrounds, lighten=True)
    if not ln_ok:
        worst = min(ln_backgrounds, key=lambda bg: contrast_ratio(out["light_neutral"], bg))
        warnings.append(
            f"light_neutral {out['light_neutral']} only reaches "
            f"{contrast_ratio(out['light_neutral'], worst):.2f}:1 over {worst} (< {AA}) -- "
            "light text can't clear AA over this fill (light primary)."
        )

    # neutral: dark body text over the finalized light_neutral. Darken to clear.
    out["neutral"], n_ok = _adjust(roles["neutral"], [out["light_neutral"]], lighten=False)
    if not n_ok:
        warnings.append(
            f"neutral {out['neutral']} only reaches "
            f"{contrast_ratio(out['neutral'], out['light_neutral']):.2f}:1 "
            f"over light_neutral {out['light_neutral']} (< {AA})."
        )

    return out, warnings


def build_wcag_schemes(source_schemes):
    """Apply adjust_scheme across a source catalog. Returns the shipped list of
    (slug, name, roles) with WCAG-nudged neutrals. Pure + idempotent."""
    return [(slug, name, adjust_scheme(roles)[0]) for slug, name, roles in source_schemes]


def scheme_report(source_schemes):
    """Per-scheme diff + AA status, for `manage.py generate_color_schemes`.
    Returns a list of dicts: {slug, name, changes: [(role, before, after)],
    warnings: [...]}"""
    report = []
    for slug, name, roles in source_schemes:
        adjusted, warnings = adjust_scheme(roles)
        changes = [
            (role, roles[role], adjusted[role])
            for role in ("light_neutral", "neutral")
            if roles[role].upper() != adjusted[role].upper()
        ]
        report.append({"slug": slug, "name": name, "changes": changes, "warnings": warnings})
    return report
