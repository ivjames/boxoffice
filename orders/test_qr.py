"""Ticket QR rendering (orders.qr), including the centered org-logo overlay.

Two layers:
  * Structural tests (always run): the overlay produces a valid PNG, differs
    from the plain code, centers the logo, and falls back to the plain QR when
    there's no logo or the logo bytes are junk.
  * A decode test that actually reads the code back with OpenCV, asserting the
    logo'd QR still resolves to the exact scan string. cv2 isn't a project
    dependency, so it's skipped where unavailable -- but it encodes the
    invariant that matters at the door and runs anywhere a decoder is present.

Out-of-band sweep behind the LOGO_FRACTION choice: decoding held 36/36 across
opaque/transparent/photographic/wide logos at scales 4-6, and only began
failing above ~0.28 -- so 0.22 keeps real margin under the error="h" budget.
"""

import io
import shutil
import tempfile

from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from PIL import Image

import segno

from dashboard.tests import DashFixtureMixin
from orders.qr import (
    LOGO_FRACTION,
    _compose_qr_with_logo,
    ticket_qr_data_uri,
    ticket_qr_png_bytes,
)
from orders.tokens import scan_code
from tenants.test_logo import image_bytes

try:
    import cv2  # noqa: F401
    import numpy as np

    _HAVE_CV2 = True
except Exception:  # pragma: no cover - depends on the host
    _HAVE_CV2 = False


def _png_size(data):
    return Image.open(io.BytesIO(data)).size


@override_settings(MEDIA_ROOT=tempfile.mkdtemp())
class TicketQrLogoTests(DashFixtureMixin, TestCase):
    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(settings.MEDIA_ROOT, ignore_errors=True)
        super().tearDownClass()

    def setUp(self):
        self.org, self.venue = self.build_org("roxy")
        _event, self.performance, _tier = self.build_ga_event(self.org, self.venue)
        self.order = self.make_paid_order(self.org, self.performance, "20.00", n_tickets=1)
        self.ticket = self.order.tickets.first()

    def _give_logo(self):
        self.org.logo = SimpleUploadedFile(
            "logo.png", image_bytes(size=(300, 300), color=(200, 20, 40)), content_type="image/png"
        )
        self.org.save()
        self.org.refresh_from_db()
        self.ticket.refresh_from_db()

    def test_plain_qr_when_org_has_no_logo(self):
        png = ticket_qr_png_bytes(self.ticket)
        self.assertTrue(png[:8] == b"\x89PNG\r\n\x1a\n")
        # No logo => identical to a bare segno render at the same params.
        buf = io.BytesIO()
        segno.make(scan_code(self.ticket), error="h").save(buf, kind="png", scale=6, border=2)
        self.assertEqual(png, buf.getvalue())

    def test_logo_overlay_changes_the_image_and_centers_a_mark(self):
        self._give_logo()
        plain = ticket_qr_png_bytes(self.ticket, logo_bytes=None)
        branded = ticket_qr_png_bytes(self.ticket)
        self.assertNotEqual(branded, plain)
        self.assertEqual(_png_size(branded), _png_size(plain))  # same footprint

        # The exact center pixel carries the red logo, not a black/white module.
        img = Image.open(io.BytesIO(branded)).convert("RGB")
        r, g, b = img.getpixel((img.width // 2, img.height // 2))
        self.assertGreater(r, 120)
        self.assertLess(g, 120)
        self.assertLess(b, 120)

    def test_bad_logo_bytes_fall_back_to_plain_qr(self):
        plain = ticket_qr_png_bytes(self.ticket, logo_bytes=None)
        got = ticket_qr_png_bytes(self.ticket, logo_bytes=b"not an image")
        self.assertEqual(got, plain)

    def test_data_uri_wraps_the_png(self):
        self._give_logo()
        uri = ticket_qr_data_uri(self.ticket)
        self.assertTrue(uri.startswith("data:image/png;base64,"))


class QrLogoScannabilityTests(TestCase):
    """Prove the logo'd code still decodes to its exact payload. Skipped where
    no QR decoder is installed (cv2 is not a project dependency)."""

    def _decode(self, png_bytes):
        arr = np.array(Image.open(io.BytesIO(png_bytes)).convert("RGB"))[:, :, ::-1].copy()
        value, _pts, _ = cv2.QRCodeDetector().detectAndDecode(arr)
        return value

    def setUp(self):
        if not _HAVE_CV2:
            self.skipTest("no QR decoder (cv2) installed")

    def test_logod_qr_decodes_to_the_exact_code(self):
        # A representative scan_code shape (10-char token + truncated HMAC) and a
        # deliberately busy logo -- the hardest case for the overlay.
        data = "ABCD123XYZ.9F3A2B7C8D1E"
        rng = np.random.default_rng(0)
        noise = (rng.random((300, 300, 3)) * 255).astype("uint8")
        logo = io.BytesIO()
        Image.fromarray(noise, "RGB").convert("RGBA").save(logo, "PNG")

        for scale in (4, 5, 6):  # email/page use 5, PDF uses 6
            png = _compose_qr_with_logo(
                segno.make(data, error="h"), logo.getvalue(), scale=scale, border=2
            )
            self.assertEqual(self._decode(png), data, f"failed to decode at scale {scale}")

    def test_fraction_is_within_the_safe_budget(self):
        self.assertLessEqual(LOGO_FRACTION, 0.26)
