from functools import wraps

from django.http import Http404


def require_tenant(view_func):
    """View decorator: 404s a request that didn't resolve to a tenant (the
    platform host / a reserved subdomain, where request.organization is
    None). Storefront views (events, performances, cart, checkout) only make
    sense scoped to one theater's subdomain — this keeps the platform host
    from ever exposing a tenant's inventory/pricing data.
    """

    @wraps(view_func)
    def wrapped(request, *args, **kwargs):
        if request.organization is None:
            raise Http404("No storefront at this host.")
        return view_func(request, *args, **kwargs)

    return wrapped
