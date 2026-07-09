from django.conf import settings
from django.http import Http404

from .models import Organization


class TenantMiddleware:
    """
    Resolves `request.organization` from the request Host header's subdomain.

    - Reserved subdomains (settings.RESERVED_SUBDOMAINS, e.g. www/app/admin)
      and a bare/absent subdomain resolve to the *platform* host:
      request.organization = None.
    - Any other subdomain must match an active Organization.subdomain, or the
      request 404s (unknown or inactive tenant) — this is the standard path;
      see the security notes below for the DEBUG-only override.
    - Everything downstream (views, templates, TenantScopedManager-based
      querysets) relies on request.organization being set before it runs, so
      this middleware must be listed after AuthenticationMiddleware but
      before anything that touches tenant data.

    Local dev override (DEBUG only):
    Real subdomains are awkward to hit from a laptop without editing
    /etc/hosts per tenant. When settings.DEBUG is True, the tenant can
    instead be selected with a `?_tenant=<subdomain>` query param or an
    `X-Tenant: <subdomain>` header, e.g.:

        http://localhost:8000/?_tenant=roxy
        curl -H "X-Tenant: roxy" http://localhost:8000/

    This override is intentionally gated on DEBUG so it can never activate in
    production regardless of what a client sends.

    Default-tenant mode (settings.DEFAULT_TENANT):
    Client subdomains are deferred for now, so the platform host needs to be
    able to serve a real storefront on its own. This does NOT change any of
    the subdomain-resolution logic above — a request that resolves to a real
    tenant subdomain behaves exactly as it always has. It only changes what
    happens for a request that would otherwise land on the platform host
    (reserved subdomain / bare host / unmatched host): if
    settings.DEFAULT_TENANT names an existing, active Organization,
    request.organization is set to that Organization (request.is_default_tenant
    = True) instead of None, so `/`, `/login`, `/dashboard`, `/scan` etc. all
    serve that org. Empty/unset/invalid DEFAULT_TENANT: behaves exactly as
    before (request.organization = None, platform landing).
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.is_default_tenant = False
        request.organization = self._resolve(request)
        return self.get_response(request)

    def _resolve(self, request):
        subdomain = self._dev_override_subdomain(request)
        if subdomain is None:
            subdomain = self._subdomain_from_host(request.get_host())

        if not subdomain or subdomain in settings.RESERVED_SUBDOMAINS:
            return self._resolve_platform_host(request)

        try:
            organization = Organization.objects.get(subdomain=subdomain)
        except Organization.DoesNotExist:
            raise Http404("Unknown tenant.")

        if not organization.is_active:
            raise Http404("This tenant is not active.")

        return organization

    def _resolve_platform_host(self, request):
        """The platform host resolution path: None unless DEFAULT_TENANT
        names an existing, active Organization, in which case that org's
        storefront is served on the platform host instead of the landing
        page. See the class docstring's "Default-tenant mode" note."""
        subdomain = (settings.DEFAULT_TENANT or "").strip()
        if not subdomain:
            return None

        organization = Organization.objects.filter(
            subdomain=subdomain, is_active=True
        ).first()
        if organization is None:
            return None

        request.is_default_tenant = True
        return organization

    def _dev_override_subdomain(self, request):
        if not settings.DEBUG:
            return None
        return request.GET.get("_tenant") or request.headers.get("X-Tenant")

    def _subdomain_from_host(self, host):
        # Strip a port, e.g. "roxy.lab980.com:8000" -> "roxy.lab980.com".
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
