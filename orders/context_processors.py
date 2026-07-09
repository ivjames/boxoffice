from . import services


def cart_count(request):
    """Expose a live `cart_count` (total held tickets) to every template for
    the nav cart badge. 0 outside a tenant/default-tenant context, or before
    the session has anything in it -- no DB hit in either of those cases."""
    organization = getattr(request, "organization", None)
    if organization is None:
        return {"cart_count": 0}
    session_key = request.session.session_key
    if not session_key:
        return {"cart_count": 0}
    return {"cart_count": services.cart_item_count(organization, session_key)}
