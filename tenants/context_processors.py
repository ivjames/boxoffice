def organization(request):
    """Expose `request.organization` to every template as `organization`."""
    return {"organization": getattr(request, "organization", None)}
