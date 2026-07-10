"""WCAG contrast helper for seat lettering -- the SERVER half of the two-
language contract mirrored by static/js/seat_contrast.js (same luminance
formula, same black-or-white tie rule). A seat's fill color is staff-chosen
(a pricing zone's color, or the neutral fallback for an unzoned seat) and can
be anything, so the row/number drawn ON the seat can't use a fixed ink color:
white text vanishes on a pale seat, black on a dark one. `text_color_rgb`
picks black or white by whichever yields the higher WCAG 2.x contrast ratio
against the seat's actual fill, so a seat's ink in the PNG/PDF export matches
what the browser draws for the very same color in the editor and online maps.

If you change the math here, change static/js/seat_contrast.js too;
events/test_seat_contrast.py hard-codes the pick for representative colors
both sides must agree on.
"""

from __future__ import annotations

BLACK_RGB = (0, 0, 0)
WHITE_RGB = (255, 255, 255)


def _channel_luminance(c):
    cs = c / 255.0
    if cs <= 0.03928:
        return cs / 12.92
    return ((cs + 0.055) / 1.055) ** 2.4


def relative_luminance(rgb):
    """WCAG relative luminance of an (r, g, b) triple (channels 0-255)."""
    r, g, b = rgb
    return 0.2126 * _channel_luminance(r) + 0.7152 * _channel_luminance(g) + 0.0722 * _channel_luminance(b)


def _contrast_ratio(l1, l2):
    hi, lo = (l1, l2) if l1 >= l2 else (l2, l1)
    return (hi + 0.05) / (lo + 0.05)


def text_color_rgb(rgb):
    """Black or white (as an (r, g, b) triple) -- whichever has the higher
    WCAG contrast ratio against the seat fill `rgb`. Ties fall to black, the
    safer default on the light neutral fill unzoned seats take. Mirrors
    static/js/seat_contrast.js's textColor()."""
    lum = relative_luminance(rgb)
    on_white = _contrast_ratio(lum, 1.0)  # white ink (luminance 1)
    on_black = _contrast_ratio(lum, 0.0)  # black ink (luminance 0)
    return WHITE_RGB if on_white > on_black else BLACK_RGB


def text_color_hex(rgb):
    """text_color_rgb as a '#rrggbb' string, for reportlab's HexColor."""
    r, g, b = text_color_rgb(rgb)
    return f"#{r:02x}{g:02x}{b:02x}"
