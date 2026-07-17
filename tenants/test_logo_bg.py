"""tenants.logo_bg: the flood-fill/rembg hybrid, session caching, and boot
warming. rembg is always mocked so these are deterministic (and fast) regardless
of whether the heavy dependency is installed. A CLEAN solid-background image is
handled by flood-fill (no rembg); only a BUSY-border image falls through to the
mocked rembg. The higher-level endpoint behavior lives in
dashboard/test_branding.py."""

import io
import sys
import types
from unittest import mock

from django.test import TestCase
from PIL import Image

import tenants.logo_bg as logo_bg
from tenants.logo_bg import BackgroundRemovalUnavailable
from tenants.test_logo import image_bytes


def _fake_rembg(on_new_session=None, on_remove=None):
    m = types.ModuleType("rembg")
    m.new_session = on_new_session or (lambda *a, **k: "SESSION")
    m.remove = on_remove or (lambda raw, session=None, **k: image_bytes(size=(120, 120)))
    return m


def _busy_bytes():
    """An image with a NON-uniform border (a gradient), so flood-fill finds no
    clean background and remove_logo_background falls through to rembg."""
    buf = io.BytesIO()
    Image.linear_gradient("L").convert("RGB").save(buf, "PNG")
    return buf.getvalue()


class FloodFillHybridTests(TestCase):
    def setUp(self):
        logo_bg._session = None
        self.addCleanup(setattr, logo_bg, "_session", None)

    def test_clean_solid_background_uses_flood_fill_not_rembg(self):
        # A solid-background image is handled by flood-fill; rembg must not be
        # touched (sabotage the session builder to prove it).
        with mock.patch.object(logo_bg, "_get_session", side_effect=AssertionError("rembg used")):
            out = logo_bg.remove_logo_background(image_bytes(size=(100, 100)))
        self.assertTrue(out.startswith(b"\x89PNG\r\n\x1a\n"))

    def test_busy_background_falls_back_to_rembg(self):
        called = []
        fake = _fake_rembg(on_remove=lambda raw, session=None, **k: (called.append(1) or image_bytes(size=(80, 80))))
        with mock.patch.dict(sys.modules, {"rembg": fake}):
            out = logo_bg.remove_logo_background(_busy_bytes())
        self.assertEqual(len(called), 1)  # rembg did the work
        self.assertTrue(out.startswith(b"\x89PNG\r\n\x1a\n"))


class LogoBgSessionTests(TestCase):
    def setUp(self):
        logo_bg._session = None  # reset the module-level cache between tests
        self.addCleanup(setattr, logo_bg, "_session", None)

    def test_session_is_built_once_and_reused_across_calls(self):
        created = []
        sessions_passed = []
        fake = _fake_rembg(
            on_new_session=lambda *a, **k: (created.append(1) or "SESSION"),
            on_remove=lambda raw, session=None, **k: (
                sessions_passed.append(session) or image_bytes(size=(80, 80))
            ),
        )
        with mock.patch.dict(sys.modules, {"rembg": fake}):
            out1 = logo_bg.remove_logo_background(_busy_bytes())  # busy -> rembg
            out2 = logo_bg.remove_logo_background(_busy_bytes())

        self.assertEqual(len(created), 1)  # session built exactly once...
        self.assertEqual(sessions_passed, ["SESSION", "SESSION"])  # ...and reused
        self.assertTrue(out1.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertTrue(out2.startswith(b"\x89PNG\r\n\x1a\n"))

    def test_warm_builds_the_session_and_is_idempotent(self):
        created = []
        fake = _fake_rembg(on_new_session=lambda *a, **k: (created.append(1) or "SESSION"))
        with mock.patch.dict(sys.modules, {"rembg": fake}):
            self.assertTrue(logo_bg.warm())
            self.assertTrue(logo_bg.warm())  # already warm -> no rebuild
        self.assertEqual(len(created), 1)

    def test_warm_returns_false_when_rembg_is_missing(self):
        # sys.modules[name] = None makes `import name` raise ImportError.
        with mock.patch.dict(sys.modules, {"rembg": None}):
            self.assertFalse(logo_bg.warm())  # never raises

    def test_busy_background_without_rembg_raises_unavailable(self):
        with mock.patch.dict(sys.modules, {"rembg": None}):
            with self.assertRaises(BackgroundRemovalUnavailable):
                logo_bg.remove_logo_background(_busy_bytes())

    def test_model_load_failure_is_unavailable_not_500(self):
        def boom(*a, **k):
            raise RuntimeError("model download blocked")

        fake = _fake_rembg(on_new_session=boom)
        with mock.patch.dict(sys.modules, {"rembg": fake}):
            self.assertFalse(logo_bg.warm())
            with self.assertRaises(BackgroundRemovalUnavailable):
                logo_bg.remove_logo_background(_busy_bytes())
