from . import services


def guest_account(request):
    """Expose the signed-in GuestAccount (or None) to every template as
    `guest_account`, so the site chrome (templates/base.html) can show a "My
    tickets" / signed-in state without every storefront view passing it
    explicitly. Reuses services.get_current_guest, which caches on the
    request and is org-scoped."""
    return {"guest_account": services.get_current_guest(request)}
