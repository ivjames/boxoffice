"""tenants.logo_flood: the dependency-free flood-fill background remover. The
key property is that it removes the edge-connected background but KEEPS interior
areas of the same colour (where the ML model over-removes)."""

import io

from django.test import TestCase
from PIL import Image, ImageDraw

from tenants.logo_flood import flood_fill_background


def _outlined_box_on_white():
    """A white canvas with a black-outlined, white-filled box in the middle --
    i.e. a white 'subject interior' enclosed by artwork, on a white background,
    not touching the edges. The hard case for ML removal."""
    im = Image.new("RGB", (120, 120), (255, 255, 255))
    d = ImageDraw.Draw(im)
    d.rectangle([30, 30, 90, 90], outline=(0, 0, 0), width=5, fill=(255, 255, 255))
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


class FloodFillBackgroundTests(TestCase):
    def test_removes_edge_background_but_keeps_interior_same_colour(self):
        result = flood_fill_background(_outlined_box_on_white())
        self.assertIsNotNone(result)
        img = Image.open(io.BytesIO(result)).convert("RGBA")
        # Corner = background -> transparent.
        self.assertEqual(img.getpixel((3, 3))[3], 0)
        # Centre = white interior enclosed by the black box -> kept opaque.
        self.assertEqual(img.getpixel((60, 60))[3], 255)
        # The outline itself is subject -> kept (its outer edge is feathered, so
        # allow a soft value rather than a hard 255).
        self.assertGreater(img.getpixel((33, 60))[3], 128)

    def test_works_on_a_solid_colour_background_not_just_white(self):
        im = Image.new("RGB", (80, 80), (12, 40, 160))  # solid blue bg
        d = ImageDraw.Draw(im)
        d.ellipse([25, 25, 55, 55], fill=(255, 200, 0))  # a distinct subject
        buf = io.BytesIO(); im.save(buf, "PNG")
        result = flood_fill_background(buf.getvalue())
        self.assertIsNotNone(result)
        img = Image.open(io.BytesIO(result)).convert("RGBA")
        self.assertEqual(img.getpixel((2, 2))[3], 0)      # blue bg gone
        self.assertEqual(img.getpixel((40, 40))[3], 255)  # yellow subject kept

    def test_busy_border_returns_none(self):
        buf = io.BytesIO()
        Image.linear_gradient("L").convert("RGB").save(buf, "PNG")  # non-uniform border
        self.assertIsNone(flood_fill_background(buf.getvalue()))

    def test_non_image_bytes_return_none(self):
        self.assertIsNone(flood_fill_background(b"not an image"))
