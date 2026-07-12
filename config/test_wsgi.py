"""Guards that config/wsgi.py serves user-uploaded MEDIA (tenant logos)
through WhiteNoise, not just static files. Without this wrapper, media is
only served under DEBUG (config/urls.py), so a tenant logo 404s in
production behind the plain proxy-to-port nginx vhost (BO-2)."""

import os
import tempfile

from django.conf import settings
from django.test import SimpleTestCase, override_settings


class WsgiMediaServingTests(SimpleTestCase):
    def _get(self, app, path):
        captured = {}

        def start_response(status, headers):
            captured["status"] = status
            captured["headers"] = dict(headers)

        env = {
            "REQUEST_METHOD": "GET",
            "PATH_INFO": path,
            "SERVER_NAME": "testserver",
            "SERVER_PORT": "80",
            "wsgi.url_scheme": "http",
            "wsgi.input": None,
            "wsgi.errors": None,
        }
        body = b"".join(app(env, start_response))
        return captured.get("status"), body

    def test_media_file_is_served_through_wsgi_app(self):
        with tempfile.TemporaryDirectory() as media_dir:
            with override_settings(MEDIA_ROOT=media_dir, MEDIA_URL="media/"):
                with open(os.path.join(media_dir, "logo.txt"), "w") as fh:
                    fh.write("brand")
                # Import here so the wrapper picks up the overridden MEDIA_ROOT.
                import importlib

                import config.wsgi as wsgi

                importlib.reload(wsgi)
                status, body = self._get(wsgi.application, "/media/logo.txt")
        self.assertEqual(status, "200 OK")
        self.assertEqual(body, b"brand")
