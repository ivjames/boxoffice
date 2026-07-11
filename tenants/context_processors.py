def organization(request):
    """Expose `request.organization` to every template as `organization`
    (None on the platform host, which serves the landing page — see
    tenants.middleware.TenantMiddleware)."""
    return {
        "organization": getattr(request, "organization", None),
    }
