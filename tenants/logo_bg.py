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

COLD START. The expensive part is NOT the removal (a normalized <=512px logo is
~2s to cut out) -- it's the one-time setup: importing rembg pulls in numba +
onnxruntime (~20s on a 2-core droplet) and building a session loads the ~170MB
model (~4s). We pay that ONCE per process by caching the session module-level,
and `warm()` lets a server pay it at worker boot (off the request path) rather
than inside the first user click, where it used to blow gunicorn's 30s worker
timeout and 500. See deploy/gunicorn.conf.py.
"""

import threading

from django.core.exceptions import ValidationError

from .logo_images import normalize_logo_bytes


class LogoBackgroundError(Exception):
    """Background removal couldn't complete. Message is user-facing."""


class BackgroundRemovalUnavailable(LogoBackgroundError):
    """The rembg dependency (and its model) isn't installed/loadable here."""


# Cached rembg session (the loaded model). Created once per process and reused
# by every removal -- building it imports rembg and loads the model, which is
# the slow part. Guarded by a lock so the boot-warm thread and a request racing
# to first-use don't both build one.
_session = None
_session_lock = threading.Lock()


def background_removal_available():
    """True if rembg can be imported here -- lets a caller/UI hide or disable
    the action instead of offering something that will always be unavailable."""
    try:
        import rembg  # noqa: F401
    except Exception:
        return False
    return True


def _get_session():
    """Return the cached rembg session, building it once. Raises
    BackgroundRemovalUnavailable if rembg can't be imported or the model can't
    be loaded (not installed, or the one-time model download failed)."""
    global _session
    if _session is not None:
        return _session
    with _session_lock:
        if _session is None:
            try:
                from rembg import new_session
            except Exception as exc:  # ImportError / broken onnxruntime
                raise BackgroundRemovalUnavailable(
                    "Background removal isn’t available on this server right now."
                ) from exc
            try:
                _session = new_session()
            except Exception as exc:  # model download blocked / load failure
                raise BackgroundRemovalUnavailable(
                    "Background removal isn’t available on this server right now."
                ) from exc
    return _session


def warm():
    """Build the rembg session ahead of the first request so the ~20s import +
    model load happens at worker boot, not inside a user's click. Never raises
    -- returns True if warmed, False if rembg/model isn't available. Called from
    the gunicorn post_worker_init hook (deploy/gunicorn.conf.py)."""
    try:
        _get_session()
        return True
    except Exception:
        return False


def remove_logo_background(raw):
    """Take the raw bytes of an image and return optimized PNG bytes with the
    background made transparent, reusing the cached session. The result is run
    back through normalize_logo_bytes so a de-boxed logo obeys the same
    size/format rules as any other upload (and a huge source can't sneak past
    the resize).

    Raises BackgroundRemovalUnavailable if rembg/model isn't available, or
    LogoBackgroundError if the model runs but the image can't be processed."""
    session = _get_session()

    from rembg import remove  # import is cheap now the session's been built

    try:
        cut_out = remove(raw, session=session)
    except Exception as exc:
        # Model/inference failure (bad image, OOM, …).
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
