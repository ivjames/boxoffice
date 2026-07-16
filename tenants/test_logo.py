"""Logo upload handling: normalization on upload (tenants.logo_images) and the
model save() choke point that applies it. Background-removal *endpoint* behavior
lives with the other branding-view tests (dashboard.test_branding); the pure
byte transform is covered here.

Uploads land in MEDIA_ROOT, so every test that saves a logo runs against a
throwaway temp dir (TempMediaMixin) -- no stray files under the real media dir.
"""

import io
import shutil
import tempfile
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from PIL import Image

from tenants.logo_images import (
    MAX_LOGO_DIMENSION,
    MAX_LOGO_UPLOAD_BYTES,
    normalize_logo_bytes,
    validate_logo_upload,
)
from venues.tests import make_org


def image_bytes(fmt="PNG", size=(1000, 600), color=(200, 30, 60), mode="RGB"):
    img = Image.new(mode, size, color)
    buf = io.BytesIO()
    img.save(buf, fmt)
    return buf.getvalue()


class NormalizeLogoBytesTests(TestCase):
    def test_downscales_to_max_dimension_and_emits_png(self):
        out = normalize_logo_bytes(image_bytes(fmt="JPEG", size=(2000, 1200)))
        img = Image.open(io.BytesIO(out))
        self.assertEqual(img.format, "PNG")
        self.assertEqual(max(img.size), MAX_LOGO_DIMENSION)
        # Aspect ratio preserved (2000:1200 == 5:3).
        self.assertEqual(img.size, (MAX_LOGO_DIMENSION, int(MAX_LOGO_DIMENSION * 0.6)))

    def test_small_logo_is_not_upscaled(self):
        out = normalize_logo_bytes(image_bytes(size=(64, 48)))
        self.assertEqual(Image.open(io.BytesIO(out)).size, (64, 48))

    def test_transparency_is_preserved(self):
        transparent = Image.new("RGBA", (100, 100), (255, 0, 0, 0))
        buf = io.BytesIO()
        transparent.save(buf, "PNG")
        out = normalize_logo_bytes(buf.getvalue())
        img = Image.open(io.BytesIO(out))
        self.assertEqual(img.mode, "RGBA")
        # The fully-transparent pixel stays transparent (alpha 0).
        self.assertEqual(img.getpixel((0, 0))[3], 0)

    def test_non_image_bytes_raise_validationerror(self):
        with self.assertRaises(ValidationError):
            normalize_logo_bytes(b"this is not an image")

    def test_oversized_dimensions_are_rejected_before_decoding(self):
        # The pixel guard rejects a bomb (huge declared dimensions) up front. We
        # prove the guard fires -- and fires from image.size, before load() --
        # by capping MAX_LOGO_PIXELS below a modest real image and asserting
        # load() is never reached.
        raw = image_bytes(size=(500, 500))  # 250k px
        with patch("tenants.logo_images.MAX_LOGO_PIXELS", 100_000):
            with patch("PIL.Image.Image.load", side_effect=AssertionError("decoded!")) as load:
                with self.assertRaises(ValidationError):
                    normalize_logo_bytes(raw)
            load.assert_not_called()


class ValidateLogoUploadTests(TestCase):
    def test_oversized_upload_is_rejected(self):
        big = SimpleUploadedFile("logo.png", b"x" * (MAX_LOGO_UPLOAD_BYTES + 1))
        with self.assertRaises(ValidationError):
            validate_logo_upload(big)

    def test_reasonable_upload_passes(self):
        ok = SimpleUploadedFile("logo.png", image_bytes(size=(300, 300)))
        validate_logo_upload(ok)  # does not raise

    def test_oversized_dimensions_are_rejected(self):
        # Regression: a huge-DIMENSION but small-BYTES image (a solid-color PNG
        # is tens of MP in a few KB) passed the byte cap + Django's image check,
        # then raised in Organization.save() as an uncaught 500. The validator
        # now catches it at form-validation time.
        big = SimpleUploadedFile("big.png", image_bytes(size=(6000, 6000)))
        self.assertLess(big.size, MAX_LOGO_UPLOAD_BYTES)  # would pass the byte cap
        with self.assertRaises(ValidationError):
            validate_logo_upload(big)

    def test_validator_rewinds_the_file(self):
        # The dimension check reads the file; it must seek back so the storage
        # save that follows still sees the whole image.
        f = SimpleUploadedFile("logo.png", image_bytes(size=(300, 300)))
        validate_logo_upload(f)
        self.assertEqual(f.tell(), 0)


@override_settings(MEDIA_ROOT=tempfile.mkdtemp())
class OrganizationLogoSaveTests(TestCase):
    @classmethod
    def tearDownClass(cls):
        from django.conf import settings

        shutil.rmtree(settings.MEDIA_ROOT, ignore_errors=True)
        super().tearDownClass()

    def test_fresh_upload_is_normalized_to_png(self):
        org = make_org("roxy")
        org.logo = SimpleUploadedFile(
            "brand.jpg", image_bytes(fmt="JPEG", size=(1600, 900)), content_type="image/jpeg"
        )
        org.save()
        org.refresh_from_db()

        self.assertTrue(org.logo.name.endswith(".png"))
        with org.logo.open("rb") as fh:
            img = Image.open(io.BytesIO(fh.read()))
        self.assertEqual(img.format, "PNG")
        self.assertLessEqual(max(img.size), MAX_LOGO_DIMENSION)

    def test_resaving_without_a_new_upload_does_not_reprocess(self):
        # A stored logo is a plain FieldFile on later saves, so re-saving the org
        # for any other reason must leave the file (name + bytes) untouched --
        # otherwise every save would re-encode and slowly degrade the logo.
        org = make_org("roxy")
        org.logo = SimpleUploadedFile(
            "brand.png", image_bytes(size=(800, 800)), content_type="image/png"
        )
        org.save()
        name_after_upload = org.logo.name
        with org.logo.open("rb") as fh:
            bytes_after_upload = fh.read()

        org.contact_email = "changed@example.com"
        org.save()
        org.refresh_from_db()

        self.assertEqual(org.logo.name, name_after_upload)
        with org.logo.open("rb") as fh:
            self.assertEqual(fh.read(), bytes_after_upload)


@override_settings(MEDIA_ROOT=tempfile.mkdtemp(), BASE_DOMAIN="boxo.test")
class StorefrontLogoHeadTests(TestCase):
    """base.html emits a per-tenant favicon + Open Graph image off the org's
    logo. Covers both the has-logo and no-logo branches on the storefront home."""

    @classmethod
    def tearDownClass(cls):
        from django.conf import settings

        shutil.rmtree(settings.MEDIA_ROOT, ignore_errors=True)
        super().tearDownClass()

    def _get_home(self):
        return self.client.get("/", HTTP_HOST="roxy.boxo.test")

    def test_logo_drives_favicon_and_og_image(self):
        org = make_org("roxy")
        org.logo = SimpleUploadedFile(
            "logo.png", image_bytes(size=(256, 256)), content_type="image/png"
        )
        org.save()
        resp = self._get_home()
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertIn('rel="apple-touch-icon"', html)
        self.assertIn('property="og:image"', html)
        # og:image must be an absolute URL built off the tenant origin.
        self.assertIn(f'content="{org.base_url}{org.logo.url}"', html)

    def test_no_logo_omits_og_image(self):
        make_org("roxy")
        resp = self._get_home()
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn('property="og:image"', resp.content.decode())
