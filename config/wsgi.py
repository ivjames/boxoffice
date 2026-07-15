"""WSGI config for the boxoffice project."""

import os

from django.conf import settings
from django.core.wsgi import get_wsgi_application

from whitenoise import WhiteNoise

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.prod")

application = get_wsgi_application()

# Serve user-uploaded MEDIA (tenant logos) through WhiteNoise, alongside the
# hashed static files the WhiteNoiseMiddleware already serves. The
# staticfiles middleware only ever serves STATIC_ROOT; this wrapper adds
# MEDIA_ROOT at MEDIA_URL so the nginx vhost stays a plain proxy-to-port with
# NO per-app location blocks -- which matters because there is one vhost per
# tenant, all proxying the same port (see deploy/nginx.sample.conf). Without
# this, config/urls.py only serves media under DEBUG, so a tenant logo 404s
# in production.
#
# autorefresh=True because media is MUTABLE (a logo can be uploaded via
# /admin after the worker started), unlike the immutable, hashed static
# files WhiteNoise is usually paired with -- it re-stats on request so a new
# upload is served without a worker restart. MEDIA_URL is stripped to a bare
# prefix ("media/" not "/media/") to match WhiteNoise's prefix expectation.
application = WhiteNoise(
    application,
    root=settings.MEDIA_ROOT,
    prefix=settings.MEDIA_URL.strip("/") + "/",
    autorefresh=True,
)
