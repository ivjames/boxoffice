"""tenants.logo_bg: session caching + boot warming. rembg is always mocked so
these are deterministic (and fast) regardless of whether the heavy dependency is
installed on the machine running the suite. The higher-level endpoint behavior
lives in dashboard/test_branding.py."""

import sys
import types
from unittest import mock

from django.test import TestCase

import tenants.logo_bg as logo_bg
from tenants.logo_bg import BackgroundRemovalUnavailable
from tenants.test_logo import image_bytes


def _fake_rembg(on_new_session=None, on_remove=None):
    m = types.ModuleType("rembg")
    m.new_session = on_new_session or (lambda *a, **k: "SESSION")
    m.remove = on_remove or (lambda raw, session=None, **k: image_bytes(size=(120, 120)))
    return m


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
            out1 = logo_bg.remove_logo_background(image_bytes(size=(100, 100)))
            out2 = logo_bg.remove_logo_background(image_bytes(size=(100, 100)))

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

    def test_missing_rembg_raises_unavailable(self):
        with mock.patch.dict(sys.modules, {"rembg": None}):
            with self.assertRaises(BackgroundRemovalUnavailable):
                logo_bg.remove_logo_background(image_bytes(size=(50, 50)))

    def test_model_load_failure_is_unavailable_not_500(self):
        def boom(*a, **k):
            raise RuntimeError("model download blocked")

        fake = _fake_rembg(on_new_session=boom)
        with mock.patch.dict(sys.modules, {"rembg": fake}):
            self.assertFalse(logo_bg.warm())
            with self.assertRaises(BackgroundRemovalUnavailable):
                logo_bg.remove_logo_background(image_bytes(size=(50, 50)))
