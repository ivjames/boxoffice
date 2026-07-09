from .permissions import get_membership


def staff_membership(request):
    """Expose the current request's staff Membership (or None) to every
    template as `staff_membership`, so the site chrome (templates/base.html)
    can show an Account/Dashboard menu for signed-in staff without every view
    having to pass it explicitly. Reuses accounts.permissions.get_membership,
    which caches on the request -- dashboard/scan views that already resolved
    a Membership via the *_required decorators/mixins don't pay for a second
    query here."""
    return {"staff_membership": get_membership(request)}
