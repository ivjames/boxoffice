"""Logo upload handling: validation + normalization for Organization.logo.

A tenant's logo is shown in a lot of small places -- the storefront header, the
dashboard nav, transactional/marketing emails, the browser favicon, and the
social-share (Open Graph) card -- none of which want a multi-megabyte,
camera-resolution original. This module is the single choke point that keeps
what we store fit for those uses:

  * `validate_logo_upload` rejects a file that's too large *before* we try to
    decode it, so a hostile or fat-fingered upload is a clean form error, not a
    memory spike. It's wired onto the model field, so BOTH the dashboard
    branding form and Django admin surface the same message.
  * `process_logo_file` runs on a freshly-uploaded file (see
    Organization.save): it fixes EXIF orientation, downscales to a sane maximum
    edge, strips metadata, and rewrites the image as an optimized PNG --
    preserving transparency, which matters for logos and is exactly what the
    background-removal endpoint (tenants/logo_bg.py) produces.

Both limits are overridable from settings for an operator who wants a different
ceiling without a code change; the defaults below are the intent.
"""

import io
import os

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile

# Largest raw upload we'll accept. Generous for a real logo (even a detailed PNG
# is well under this), but bounds what a single request can throw at Pillow.
MAX_LOGO_UPLOAD_BYTES = getattr(settings, "MAX_LOGO_UPLOAD_BYTES", 5 * 1024 * 1024)

# Longest edge we keep. A logo never needs more than this for any of its uses
# (header, favicon, email, OG card); downscaling here is what turns a
# camera-sized original into a few-KB asset. Aspect ratio is preserved.
MAX_LOGO_DIMENSION = getattr(settings, "MAX_LOGO_DIMENSION", 512)

# Cap on DECODED pixels. The byte limit above bounds the compressed upload, not
# the image it expands to: a tiny PNG/WebP can declare hundreds of millions of
# pixels (a "decompression bomb"), and Pillow only errors above ~178M pixels
# (2x its warn threshold) -- so a crafted file in between would allocate its
# full RGBA buffer in memory before thumbnail() could shrink it. We read the
# header-declared size first (cheap) and reject before decoding. 25 MP is far
# beyond any real logo yet caps a decode at ~100 MB.
MAX_LOGO_PIXELS = getattr(settings, "MAX_LOGO_PIXELS", 25_000_000)


def read_logo_bytes(organization):
    """Return the raw bytes of an org's logo, or None if it has none / the file
    can't be read. Central helper so the surfaces that embed the logo (the ticket
    PDF header, the QR overlay) read it one way and never raise on a missing or
    broken file -- an unbranded fallback beats a crash."""
    logo = getattr(organization, "logo", None)
    if not logo:
        return None
    try:
        logo.open("rb")
        try:
            return logo.read()
        finally:
            logo.close()
    except Exception:
        return None


def validate_logo_upload(file):
    """Field validator: refuse a logo whose raw bytes exceed
    MAX_LOGO_UPLOAD_BYTES. Runs during form/model full_clean (dashboard branding
    form + admin), so an oversized file is a friendly field error rather than a
    processing failure. Image-ness itself is already enforced by ImageField."""
    size = getattr(file, "size", None)
    if size is not None and size > MAX_LOGO_UPLOAD_BYTES:
        limit_mb = MAX_LOGO_UPLOAD_BYTES / (1024 * 1024)
        raise ValidationError(
            f"That logo is too large ({size / (1024 * 1024):.1f} MB). "
            f"Please upload an image under {limit_mb:.0f} MB."
        )


def normalize_logo_bytes(raw):
    """Return optimized PNG bytes for `raw` (the bytes of an uploaded image):
    EXIF-oriented, downscaled so the longest edge is <= MAX_LOGO_DIMENSION,
    metadata stripped, transparency preserved. Raises ValidationError if the
    bytes aren't a decodable image. Pure bytes->bytes so it's reusable by the
    background-removal endpoint and easy to test."""
    from PIL import Image, ImageOps, UnidentifiedImageError

    # open() reads only the header, so image.size is known WITHOUT decoding the
    # pixels. Reject an over-large image here, before load() allocates its full
    # buffer -- this is the decompression-bomb guard (see MAX_LOGO_PIXELS).
    try:
        image = Image.open(io.BytesIO(raw))
    except (UnidentifiedImageError, OSError) as exc:
        raise ValidationError("That file couldn't be read as an image.") from exc

    width, height = image.size
    if width * height > MAX_LOGO_PIXELS:
        megapixels = MAX_LOGO_PIXELS / 1_000_000
        raise ValidationError(
            f"That image is too large ({width}×{height}px). "
            f"Please upload a logo under {megapixels:.0f} megapixels."
        )

    try:
        image.load()
    except (OSError, Image.DecompressionBombError) as exc:
        raise ValidationError("That file couldn't be read as an image.") from exc

    # Respect the camera/orientation tag, then drop it (and all other metadata)
    # by rebuilding the image below. RGBA keeps any transparency; a flat photo
    # just becomes fully opaque.
    image = ImageOps.exif_transpose(image)
    if image.mode != "RGBA":
        image = image.convert("RGBA")

    # thumbnail() only ever shrinks (never upscales a small logo) and preserves
    # aspect ratio, so a wordmark stays a wordmark.
    image.thumbnail((MAX_LOGO_DIMENSION, MAX_LOGO_DIMENSION), Image.LANCZOS)

    out = io.BytesIO()
    image.save(out, format="PNG", optimize=True)
    return out.getvalue()


def process_logo_file(fieldfile):
    """Normalize the image a FieldFile currently points at and rewrite it in
    place as an optimized PNG. Called from Organization.save() for a fresh
    upload; assigns back with save=False (no recursion -- the outer save writes
    the row). The stored name keeps the upload's stem so it stays recognizable
    in /media/org_logos/."""
    fieldfile.open("rb")
    try:
        raw = fieldfile.read()
    finally:
        fieldfile.close()

    png = normalize_logo_bytes(raw)

    stem = os.path.splitext(os.path.basename(fieldfile.name or "logo"))[0] or "logo"
    fieldfile.save(f"{stem}.png", ContentFile(png), save=False)
