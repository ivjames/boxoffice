"""Tests for events/seat_contrast.py -- the server half of the WCAG seat-
lettering contrast contract (mirrored by static/js/seat_contrast.js). Locks
the black-or-white pick for representative seat fills so the PNG/PDF export
(events/zone_export.py) and the browser can never drift apart, and asserts
the two picks each clear WCAG AA (4.5:1) against the seat fill."""

from django.test import SimpleTestCase

from events.seat_contrast import (
    BLACK_RGB,
    WHITE_RGB,
    relative_luminance,
    text_color_hex,
    text_color_rgb,
)


def _contrast(a, b):
    la, lb = relative_luminance(a), relative_luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


class SeatContrastTests(SimpleTestCase):
    def test_dark_fills_get_white_ink(self):
        for rgb in [(0, 0, 0), (30, 58, 138), (17, 24, 39), (225, 29, 72)]:
            self.assertEqual(text_color_rgb(rgb), WHITE_RGB, rgb)

    def test_light_fills_get_black_ink(self):
        # White, the neutral unzoned fill (#d1d5db), the inert fill (#e5e7eb),
        # and a bright yellow zone all read better with black ink.
        for rgb in [(255, 255, 255), (209, 213, 219), (229, 231, 235), (250, 204, 21)]:
            self.assertEqual(text_color_rgb(rgb), BLACK_RGB, rgb)

    def test_pick_always_beats_the_alternative(self):
        # Whatever it picks must out-contrast the other choice for every fill.
        for rgb in [(0, 0, 0), (128, 128, 128), (255, 255, 255), (250, 204, 21), (30, 58, 138)]:
            chosen = text_color_rgb(rgb)
            other = BLACK_RGB if chosen == WHITE_RGB else WHITE_RGB
            self.assertGreaterEqual(_contrast(rgb, chosen), _contrast(rgb, other), rgb)

    def test_mid_gray_tie_falls_to_black(self):
        # 0.05 luminance sits either side of the tie; a true mid gray defaults
        # to black per the documented tie rule.
        self.assertEqual(text_color_rgb((119, 119, 119)), BLACK_RGB)

    def test_hex_helper_matches_rgb(self):
        self.assertEqual(text_color_hex((0, 0, 0)), "#ffffff")
        self.assertEqual(text_color_hex((255, 255, 255)), "#000000")
