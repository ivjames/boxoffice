"""Color-scheme generator: harmonious accents, WCAG neutrals, dark variants.

The 36 built-in schemes (tenants.color_schemes.SOURCE_SCHEMES) carry the
design's brand colors. This module takes that source and produces the shipped
`BUILTIN_SCHEMES` (`derive_scheme` per entry) in two steps:

  1. `harmonize_accent` -- re-derive the `feature_accent` as an ANALOGOUS
     neighbor of the primary's hue (a small rotation on the wheel), replacing
     the source's clashing near-complementary accent. Primary / secondary /
     dark_accent are never touched.
  2. `adjust_scheme` -- shift ONLY the two neutral/text roles so they clear WCAG
     contrast against the surfaces they sit on (the "best-of-two" contract
     below). The brand fills, including the just-harmonized accent, pass through.

`dark_surfaces` additionally derives each scheme's dark-theme page surfaces
(branded near-black bg, light text) so the storefront ships a dark variant; the
brand fills and their on-colors are shared across both themes.

WCAG contract ("best-of-two per surface"):

Each surface (the four brand fills, plus the light_neutral page background) is
labelled light or dark by its relative luminance (WCAG's black/white crossover,
TEXT_LUMINANCE_THRESHOLD). Text over a light fill is the DARK neutral; text over
a dark fill is the LIGHT neutral -- the two neutrals swap by surface. Then:

  * `light_neutral` (light text) is lightened until it clears the target over
    every DARK brand fill.
  * `neutral` (dark text) is darkened until it clears the target over every
    LIGHT brand fill AND over the light_neutral page background.

Because the threshold is exactly the point where black/white text hits ~4.58:1,
pushing the chosen neutral toward white/black always clears AA -- so every
scheme is reachable (no light-primary dead ends). Only luminance moves: the
adjustment shifts HSL lightness while holding hue and saturation, so a
"Champagne Cream" stays cream, just a shade lighter/darker, and most neutrals
barely move (they already pass). Target is WCAG AA (4.5:1), upgraded to AAA
(7:1) when reaching it costs only a small extra nudge (AAA_CHEAP_DELTA_L). The
generation is pure and idempotent.
"""

import colorsys

# WCAG 2.x contrast thresholds for text.
AA = 4.5
AAA = 7.0

# Relative-luminance crossover where black text and white text over a fill give
# equal contrast (~4.58:1). Above it a fill takes DARK text (the `neutral`
# role); at or below it, LIGHT text (`light_neutral`). Derives from solving
# (L+0.05)/0.05 = 1.05/(L+0.05).
TEXT_LUMINANCE_THRESHOLD = 0.1791

# Upgrade an AA-passing color to AAA only when the extra HSL-lightness move
# beyond the AA solution is at most this much -- i.e. AAA is "cheap" here.
AAA_CHEAP_DELTA_L = 0.08

# How finely we scan lightness looking for the smallest move that clears the
# target. 1/256 steps is well below 8-bit color resolution.
_SCAN_STEPS = 256


# --- color math -----------------------------------------------------------


def _to_rgb(hex_color):
    h = hex_color.lstrip("#")
    if len(h) == 3:  # #abc shorthand -- the hex validator accepts it, so expand
        h = "".join(ch * 2 for ch in h)
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


def _hls(hex_color):
    """(hue, lightness, saturation) of a hex color, each in 0..1."""
    r, g, b = _to_rgb(hex_color)
    return colorsys.rgb_to_hls(r, g, b)


def _from_hls(h, l, s):
    """Hex for an HLS triple, hue wrapped and lightness/saturation clamped."""
    return _to_hex(colorsys.hls_to_rgb(h % 1.0, min(1.0, max(0.0, l)), min(1.0, max(0.0, s))))


def _clamp(value, lo, hi):
    return max(lo, min(hi, value))


def _lightness(hex_color):
    return _hls(hex_color)[1]


def _with_lightness(hex_color, lightness):
    """The color with its HSL lightness replaced (hue + saturation held)."""
    h, _l, s = _hls(hex_color)
    return _from_hls(h, lightness, s)


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


def _is_light(hex_color):
    """True if a fill takes dark text (its luminance is above the black/white
    crossover), False if it takes light text."""
    return relative_luminance(hex_color) > TEXT_LUMINANCE_THRESHOLD


def text_over(fill, light_neutral, neutral):
    """The neutral to use as legible text over `fill` (best-of-two): the dark
    `neutral` on a light fill, the light `light_neutral` on a dark fill. The
    same rule the generator guarantees AA for, so a storefront that colors text
    with this over each surface is accessible by construction."""
    return neutral if _is_light(fill) else light_neutral


def readable_on(color, background, target=AA):
    """`color` nudged (HSL lightness, hue + saturation held) until it's legible
    AS TEXT on `background` -- darkened on a light background, lightened on a
    dark one. Returned unchanged when it already clears `target`. Used to make
    brand-colored headings/links ('ink') stay readable on the page background:
    a pale primary becomes a deeper shade of the same hue for text, while the
    fill keeps the exact brand color."""
    if contrast_ratio(color, background) >= target:
        return color
    adjusted, _ok = _search(color, [background], target, lighten=not _is_light(background))
    return adjusted


# --- harmonious accent derivation -----------------------------------------
#
# The source schemes historically paired a warm primary with a cool, near-
# complementary feature accent (e.g. burgundy + teal). That maximises contrast
# but reads as a clash. Instead we DERIVE the feature accent as an *analogous*
# neighbor of the primary's hue -- a small rotation on the color wheel -- so the
# accent stays in the same warm/cool family as the primary and feels intentional
# rather than jarring, while still popping as a call-to-action.

# Analogous rotation from the primary hue (degrees). ~+34 keeps the accent a
# close neighbor (reds -> warm orange/gold, blues -> indigo/violet) rather than
# the opposite side of the wheel.
ACCENT_HUE_ROTATION = 34 / 360.0

# Accent hues in this band read as muddy olive/khaki. A +rotation from a gold or
# yellow primary lands here; when it does we rotate the OTHER way instead (toward
# warm amber/orange), which is the more flattering analogous neighbor anyway.
_MUDDY_ACCENT_BAND = (54 / 360.0, 92 / 360.0)


def _analogous_hue(base_h):
    """The primary hue rotated by an analogous step, away from the muddy
    olive/khaki band when a +rotation would otherwise land in it."""
    plus = (base_h + ACCENT_HUE_ROTATION) % 1.0
    lo, hi = _MUDDY_ACCENT_BAND
    if lo <= plus <= hi:
        return (base_h - ACCENT_HUE_ROTATION) % 1.0
    return plus

# A primary this desaturated (grey / cream / near-black) has no hue worth
# harmonizing with -- rotating it just yields another grey. Below this floor we
# harmonize off the scheme's most chromatic brand role instead, and if the whole
# scheme is neutral we keep the source's curated accent pop.
ACCENT_MIN_SATURATION = 0.20

# Where the derived accent is re-seated. Saturation is pulled into a vivid-but-
# tasteful band; lightness starts at a mid value that reads as an accent on the
# light page, then moves as needed to separate from the primary fill.
ACCENT_SAT_RANGE = (0.46, 0.74)
ACCENT_TARGET_L = 0.47

# The accent fill must be visibly distinct from the primary fill. HSL-lightness
# distance isn't enough (a yellow and a blue can share a lightness yet differ
# wildly in luminance, or vice-versa), so we require a real WCAG contrast ratio
# between the two fills and move the accent's lightness until it's met.
ACCENT_MIN_CONTRAST_VS_PRIMARY = 2.0


def _accent_lightness(hue, sat, primary):
    """Lightness for the accent so it clears ACCENT_MIN_CONTRAST_VS_PRIMARY
    against the primary fill, starting from ACCENT_TARGET_L and taking the
    smallest move (lighter or darker) that gets there. Falls back to the more
    contrasting extreme if neither direction reaches the target."""
    if contrast_ratio(_from_hls(hue, ACCENT_TARGET_L, sat), primary) >= ACCENT_MIN_CONTRAST_VS_PRIMARY:
        return ACCENT_TARGET_L
    best_l, best_c = ACCENT_TARGET_L, 0.0
    for step in range(1, 51):
        for bound in (1.0, 0.0):  # try lighter and darker at each distance
            l = ACCENT_TARGET_L + (bound - ACCENT_TARGET_L) * (step / 50)
            c = contrast_ratio(_from_hls(hue, l, sat), primary)
            if c >= ACCENT_MIN_CONTRAST_VS_PRIMARY:
                return l
            if c > best_c:
                best_l, best_c = l, c
    return best_l


def harmonize_accent(roles):
    """Return `roles` with `feature_accent` replaced by an analogous neighbor of
    the primary (see the module constants). Only the accent changes; every other
    role -- including the primary itself -- passes through untouched. A scheme
    whose brand roles are all near-neutral keeps its source accent."""
    candidates = [roles["primary"], roles["secondary"], roles["dark_accent"]]
    base = next((c for c in candidates if _hls(c)[2] >= ACCENT_MIN_SATURATION), None)
    if base is None:
        return dict(roles)  # intentionally-neutral scheme -- keep the curated pop
    accent_h = _analogous_hue(_hls(base)[0])
    accent_s = _clamp(_hls(base)[2], *ACCENT_SAT_RANGE)
    accent_l = _accent_lightness(accent_h, accent_s, roles["primary"])
    out = dict(roles)
    out["feature_accent"] = _from_hls(accent_h, accent_l, accent_s)
    return out


# --- dark-theme derivation -------------------------------------------------
#
# Every scheme also ships a dark variant. The brand FILLS (primary / secondary /
# accent / dark_accent) and their on-colors are unchanged -- a button stays the
# same brand color in either theme -- but the page SURFACES flip: a branded
# near-black background, light text, and brand "ink" colors lightened so
# headings/links stay legible on the dark page.

_DARK_BG_L = 0.09        # page background lightness (branded near-black)
_DARK_SUBTLE_L = 0.12    # barely-raised surface (subtle striping / insets)
_DARK_SURFACE_L = 0.16   # cards / raised surfaces, a clear step up from the page
_DARK_MUTED_SURF_L = 0.22  # inset chips / muted fills
_DARK_BORDER_L = 0.28    # hairline borders
_DARK_MUTED_L = 0.68     # secondary/muted text
_DARK_TINT_S = 0.22      # cap on how much the primary hue tints the dark neutrals


def dark_surfaces(roles):
    """The dark-theme page surfaces for a scheme, tinted with the primary's hue
    so the dark storefront still feels branded rather than a flat grey. Returns
    bg / surface / surface_subtle / surface_muted / text / muted / border. Pure;
    depends only on `roles`."""
    hue, _l, sat = _hls(roles["primary"])
    tint = min(sat, _DARK_TINT_S)
    return {
        "bg": _from_hls(hue, _DARK_BG_L, tint),
        "surface": _from_hls(hue, _DARK_SURFACE_L, tint),
        "surface_subtle": _from_hls(hue, _DARK_SUBTLE_L, tint),
        "surface_muted": _from_hls(hue, _DARK_MUTED_SURF_L, tint),
        "text": roles["light_neutral"],  # already lightened for contrast on dark
        "muted": _from_hls(hue, _DARK_MUTED_L, min(sat, 0.12)),
        "border": _from_hls(hue, _DARK_BORDER_L, tint),
    }


def dark_ink(color, dark_bg):
    """A brand color lightened until it's legible AS TEXT on the dark page
    background -- the dark-mode counterpart of `readable_on` for a light page."""
    return readable_on(color, dark_bg)


# --- storefront page tint --------------------------------------------------
#
# The light page background can carry far more brand presence than the near-
# white `light_neutral` role, which stays pale because it doubles as the light-
# on-dark text color. So we derive a SEPARATE page background that leans on the
# scheme's brand hue, at a tenant-chosen intensity, bounded so the near-black
# body text still clears WCAG AAA over it -- presence never costs legibility.

# (saturation, lightness) per intensity: more saturation + lower lightness =
# more presence. Tuned for a brand hue (more chromatic than a neutral), so the
# page reads as a confident tint rather than a loud wash. "none" is handled
# separately (returns the untinted light_neutral -- today's near-white look).
PAGE_TINT_LEVELS = {
    "subtle": (0.20, 0.94),
    "medium": (0.34, 0.89),
    "bold": (0.48, 0.83),
}


def _brand_hue(palette):
    """The hue to lean the page tint on: the first sufficiently-chromatic brand
    role (primary, then secondary / dark_accent / feature_accent), falling back
    to light_neutral's own hue for an all-neutral scheme."""
    for role in ("primary", "secondary", "dark_accent", "feature_accent"):
        if _hls(palette[role])[2] >= ACCENT_MIN_SATURATION:
            return _hls(palette[role])[0]
    return _hls(palette["light_neutral"])[0]


def page_background(palette, level, *, floor=AAA):
    """The storefront page background at tint intensity `level` (a
    PAGE_TINT_LEVELS key, or anything else -- e.g. "none" -- for the untinted
    light_neutral). The tint leans on the scheme's brand hue and is lightened
    just enough that the dark `neutral` body text clears `floor` (AAA), so more
    presence never drops the page below AAA. Pure."""
    target = PAGE_TINT_LEVELS.get(level)
    if target is None:
        return palette["light_neutral"]
    hue = _brand_hue(palette)
    sat, lightness = target
    neutral = palette["neutral"]
    bg = _from_hls(hue, lightness, sat)
    while contrast_ratio(neutral, bg) < floor and lightness < 1.0:
        lightness += 0.004
        bg = _from_hls(hue, lightness, sat)
    return bg


def adjust_scheme(roles):
    """Return (adjusted_roles, warnings) for one scheme's role dict under the
    best-of-two contract. Only `light_neutral` and `neutral` may change; brand
    roles pass through untouched. `warnings` is normally empty (every surface is
    reachable) but is kept for defensiveness against pathological inputs."""
    out = dict(roles)
    warnings = []
    brand_fills = [roles["primary"], roles["secondary"], roles["feature_accent"], roles["dark_accent"]]
    dark_fills = [c for c in brand_fills if not _is_light(c)]
    light_fills = [c for c in brand_fills if _is_light(c)]

    # light_neutral is the LIGHT text over dark fills -> lighten until it clears.
    if dark_fills:
        out["light_neutral"], ok = _adjust(roles["light_neutral"], dark_fills, lighten=True)
        if not ok:
            worst = min(dark_fills, key=lambda bg: contrast_ratio(out["light_neutral"], bg))
            warnings.append(
                f"light_neutral {out['light_neutral']} only reaches "
                f"{contrast_ratio(out['light_neutral'], worst):.2f}:1 over {worst} (< {AA})."
            )

    # neutral is the DARK text over light fills + the light_neutral page bg
    # -> darken until it clears over all of them.
    neutral_fills = light_fills + [out["light_neutral"]]
    out["neutral"], ok = _adjust(roles["neutral"], neutral_fills, lighten=False)
    if not ok:
        worst = min(neutral_fills, key=lambda bg: contrast_ratio(out["neutral"], bg))
        warnings.append(
            f"neutral {out['neutral']} only reaches "
            f"{contrast_ratio(out['neutral'], worst):.2f}:1 over {worst} (< {AA})."
        )

    return out, warnings


def derive_scheme(roles):
    """The full generator pipeline for one scheme: harmonize the feature accent
    (analogous to the primary), then WCAG-nudge the two neutrals against the
    resulting fills. Returns (roles, warnings). Pure + idempotent."""
    return adjust_scheme(harmonize_accent(roles))


def build_wcag_schemes(source_schemes):
    """Apply the generator pipeline across a source catalog. Returns the shipped
    list of (slug, name, roles) with a harmonized accent and WCAG-nudged
    neutrals. Pure + idempotent."""
    return [(slug, name, derive_scheme(roles)[0]) for slug, name, roles in source_schemes]


def scheme_report(source_schemes):
    """Per-scheme diff + AA status, for `manage.py generate_color_schemes`.
    Returns a list of dicts: {slug, name, changes: [(role, before, after)],
    warnings: [...]}. Reports the harmonized accent alongside the neutral nudges."""
    report = []
    for slug, name, roles in source_schemes:
        adjusted, warnings = derive_scheme(roles)
        changes = [
            (role, roles[role], adjusted[role])
            for role in ("feature_accent", "light_neutral", "neutral")
            if roles[role].upper() != adjusted[role].upper()
        ]
        report.append({"slug": slug, "name": name, "changes": changes, "warnings": warnings})
    return report
