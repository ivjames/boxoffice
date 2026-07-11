from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings
from django.http import Http404
from django.utils import timezone

from .models import Organization


class TenantMiddleware:
    """
    Resolves `request.organization` from the request Host header's subdomain.

    - Reserved subdomains (settings.RESERVED_SUBDOMAINS, e.g. www/app/admin)
      and a bare/absent subdomain resolve to the *platform* host:
      request.organization = None (which serves the landing page).
    - Any other subdomain must match an active Organization.subdomain, or the
      request 404s (unknown or inactive tenant) — this is the standard path;
      see the security note below for the DEBUG-only override.
    - Everything downstream (views, templates, TenantScopedManager-based
      querysets) relies on request.organization being set before it runs, so
      this middleware must be listed after AuthenticationMiddleware but
      before anything that touches tenant data.

    Timezone: datetimes are stored UTC-aware (USE_TZ), but a showtime is a
    fact about a *place* — "8:00 PM at the Roxy" is 8pm in the theater's own
    zone for every visitor, wherever they browse from. So for a tenant request
    this activates request.organization.timezone, making every `{{ …|date }}`
    template render and every `timezone.localtime()` resolve to the venue's
    local time. The platform host (no tenant) and anything else falls back to
    settings.TIME_ZONE. The active zone is thread-local and threads are reused,
    so it is always deactivated after the response to avoid leaking one
    tenant's zone into the next request on the same worker thread.

    Local dev override (DEBUG only):
    Real subdomains are awkward to hit from a laptop without editing
    /etc/hosts per tenant. When settings.DEBUG is True, the tenant can
    instead be selected with a `?_tenant=<subdomain>` query param or an
    `X-Tenant: <subdomain>` header, e.g.:

        http://localhost:8000/?_tenant=roxy
        curl -H "X-Tenant: roxy" http://localhost:8000/

    This override is intentionally gated on DEBUG so it can never activate in
    production regardless of what a client sends.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.organization = self._resolve(request)
        self._activate_timezone(request.organization)
        try:
            return self.get_response(request)
        finally:
            # Thread-local; reset so the next request on this worker starts
            # from settings.TIME_ZONE rather than the last tenant's zone.
            timezone.deactivate()

    def _activate_timezone(self, organization):
        """Activate the tenant's local timezone for this request so all times
        render venue-local. No tenant (platform host) or an unusable/blank zone
        string falls back to settings.TIME_ZONE (leave the default active)."""
        if organization is None or not organization.timezone:
            timezone.deactivate()
            return
        try:
            timezone.activate(ZoneInfo(organization.timezone))
        except (ZoneInfoNotFoundError, ValueError):
            # Timezone is a free-text field today, so it can hold a typo like
            # "Amerca/New_York"; don't 500 the whole tenant over it — fall back
            # to the default zone. (Also the case for making it a validated
            # dropdown, per the admin cleanup.)
            timezone.deactivate()

    def _resolve(self, request):
        subdomain = self._dev_override_subdomain(request)
        if subdomain is None:
            subdomain = self._subdomain_from_host(request.get_host())

        if not subdomain or subdomain in settings.RESERVED_SUBDOMAINS:
            # Platform host (reserved subdomain / bare host / unmatched host):
            # no tenant, so the landing page is served.
            return None

        try:
            organization = Organization.objects.get(subdomain=subdomain)
        except Organization.DoesNotExist:
            raise Http404("Unknown tenant.")

        if not organization.is_active:
            raise Http404("This tenant is not active.")

        return organization

    def _dev_override_subdomain(self, request):
        if not settings.DEBUG:
            return None
        return request.GET.get("_tenant") or request.headers.get("X-Tenant")

    def _subdomain_from_host(self, host):
        # Strip a port, e.g. "roxy.boxo.show:8000" -> "roxy.boxo.show".
        host = host.split(":")[0].lower()
        base_domain = settings.BASE_DOMAIN.lower()

        if host == base_domain:
            return ""  # bare base domain -> platform host

        suffix = f".{base_domain}"
        if host.endswith(suffix):
            return host[: -len(suffix)]

        # Host doesn't belong to our base domain at all (e.g. "localhost",
        # "127.0.0.1", or an IP hit directly) -> treat as the platform host
        # rather than raising, so plain `runserver` + a browser hitting
        # localhost works out of the box.
        return ""
