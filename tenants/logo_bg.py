"""One-click background removal for a tenant's logo.

Theaters routinely upload a logo that was exported on a white (or off-white)
rectangle. That reads fine on a white page but not against the storefront's
themed/tinted background, the dark theme, or a colored email header -- the logo
sits in an obvious box. This module isolates the subject and returns a
transparent PNG, so the same logo drops cleanly onto any surface.

The heavy lifting is `rembg` (a U^2-Net segmentation model via onnxruntime).
That's a large, optional dependency: like Sentry (config/observability.py) the
import is deferred and guarded, so a deploy that doesn't install it -- and the
test suite, which mocks this -- runs perfectly well; the feature just reports
itself unavailable. Callers catch `LogoBackgroundError` (base) and, if they
want to distinguish "not installed" from "couldn't process this image",
`BackgroundRemovalUnavailable`.
"""

from django.core.exceptions import ValidationError

from .logo_images import normalize_logo_bytes


class LogoBackgroundError(Exception):
    """Background removal couldn't complete. Message is user-facing."""


class BackgroundRemovalUnavailable(LogoBackgroundError):
    """The rembg dependency (and its model) isn't installed on this host."""


def background_removal_available():
    """True if rembg can be imported here -- lets a caller/UI hide or disable
    the action instead of offering something that will always 503."""
    try:
        import rembg  # noqa: F401
    except Exception:
        return False
    return True


def remove_logo_background(raw):
    """Take the raw bytes of an image and return optimized PNG bytes with the
    background made transparent. The result is run back through
    normalize_logo_bytes so a de-boxed logo obeys the same size/format rules as
    any other upload (and a huge source can't sneak past the resize).

    Raises BackgroundRemovalUnavailable if rembg isn't installed, or
    LogoBackgroundError if the model runs but the image can't be processed."""
    try:
        from rembg import remove
    except Exception as exc:  # ImportError, or a broken onnxruntime install
        raise BackgroundRemovalUnavailable(
            "Background removal isn’t available on this server right now."
        ) from exc

    try:
        cut_out = remove(raw)
    except Exception as exc:
        # Model/inference failure (bad image, model download blocked, OOM, …).
        raise LogoBackgroundError(
            "We couldn’t remove the background from that image. "
            "Try a different logo file."
        ) from exc

    try:
        return normalize_logo_bytes(cut_out)
    except ValidationError as exc:
        raise LogoBackgroundError(
            "The background was removed but the result couldn’t be saved."
        ) from exc
