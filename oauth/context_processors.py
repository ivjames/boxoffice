from .providers import enabled_providers


def oauth_providers(request):
    """Expose the configured OAuth providers to every template as
    `oauth_providers`, so the staff-login and guest-portal sign-in pages can
    render a "Continue with …" button per enabled provider without each view
    passing them. Empty (no buttons) until an operator sets provider
    credentials -- see oauth.providers.enabled_providers()."""
    return {"oauth_providers": enabled_providers()}
