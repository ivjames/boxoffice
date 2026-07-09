def organization(request):
    """Expose `request.organization` to every template as `organization`,
    plus `is_default_tenant` (True when that organization was resolved via
    settings.DEFAULT_TENANT rather than an explicit tenant subdomain — see
    tenants.middleware.TenantMiddleware) so templates can tell the two apart
    if they ever need to (e.g. a "you're on the shared host" hint)."""
    return {
        "organization": getattr(request, "organization", None),
        "is_default_tenant": getattr(request, "is_default_tenant", False),
    }
