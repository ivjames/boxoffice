"""Shared staff-auth gating: built on Membership's role helpers
(accounts/models.py), used by BOTH the dashboard app and the scanning app so
there's exactly one place that decides "is this request allowed into the
staff area." Function-based views use the `*_required` decorators;
class-based views use the `*RequiredMixin` mixins. Both funnel through
`resolve_staff_membership()` below so the auth/role logic itself is never
duplicated.

Gate order (all four checks matter for the Phase 5 threat model):
  1. request.organization is not None -- no dashboard/scan UI on the
     platform host or an unknown subdomain (mirrors tenants.decorators.require_tenant).
  2. request.user.is_authenticated -- anonymous visitors get sent to login.
  3. A Membership row exists for (request.user, request.organization) --
     this is what blocks a real, logged-in user of Org A from reaching Org
     B's dashboard just because their session cookie is still valid there.
     Deliberately re-checked on EVERY request (not just at login time),
     because a session cookie by itself says nothing about which tenant it's
     valid for.
  4. role_check(membership) -- the role-gated variants (manager+, box
     office+, scanner+) reuse Membership's own can_*()/is_*() helpers rather
     than re-encoding the role hierarchy here.
"""

from functools import wraps

from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied
from django.http import Http404
from django.urls import reverse

from .models import Membership


class LoginRequired(Exception):
    """Internal signal: the request needs to be bounced to the login page.
    Not raised past resolve_staff_membership()'s callers below."""


def get_membership(request):
    """The Membership for request.user in request.organization, or None.
    Cached on the request object so a view can call this (or the mixins/
    decorators below) more than once per request without extra queries."""
    if request.organization is None or not request.user.is_authenticated:
        return None
    if not hasattr(request, "_membership_cache"):
        request._membership_cache = (
            Membership.objects.filter(user=request.user, organization=request.organization)
            .select_related("organization", "user")
            .first()
        )
    return request._membership_cache


def resolve_staff_membership(request, role_check=None):
    """Returns the caller's Membership if allowed, else raises:
    - Http404 if there's no tenant at this host,
    - LoginRequired if the user isn't authenticated,
    - PermissionDenied if they're authenticated but lack a Membership (or
      have one that doesn't pass `role_check`) for THIS organization.
    """
    if request.organization is None:
        raise Http404("No staff area at this host.")
    if not request.user.is_authenticated:
        raise LoginRequired()
    membership = get_membership(request)
    if membership is None:
        raise PermissionDenied("Your account doesn't have access to this theater.")
    if role_check is not None and not role_check(membership):
        raise PermissionDenied("Your role doesn't have access to this area.")
    return membership


def _require_role(role_check):
    def decorator(view_func):
        @wraps(view_func)
        def wrapped(request, *args, **kwargs):
            try:
                membership = resolve_staff_membership(request, role_check)
            except LoginRequired:
                return redirect_to_login(request.get_full_path(), reverse("login"))
            request.membership = membership
            return view_func(request, *args, **kwargs)

        return wrapped

    return decorator


# --- function-view decorators -------------------------------------------

tenant_staff_required = _require_role(None)
manager_required = _require_role(lambda m: m.can_manage_events())
box_office_required = _require_role(lambda m: m.can_sell_tickets())
scanner_required = _require_role(lambda m: m.can_scan())


# --- class-based-view mixins ---------------------------------------------


class TenantStaffRequiredMixin:
    """Any Membership (any role) in request.organization. Subclass and set
    `role_check` (a callable(membership) -> bool) for a role-gated variant --
    see ManagerRequiredMixin etc. below."""

    role_check = None

    def dispatch(self, request, *args, **kwargs):
        try:
            membership = resolve_staff_membership(request, self.role_check)
        except LoginRequired:
            return redirect_to_login(request.get_full_path(), reverse("login"))
        request.membership = membership
        self.membership = membership
        return super().dispatch(request, *args, **kwargs)


class ManagerRequiredMixin(TenantStaffRequiredMixin):
    """owner/manager -- event & performance CRUD."""

    role_check = staticmethod(lambda m: m.can_manage_events())


class BoxOfficeRequiredMixin(TenantStaffRequiredMixin):
    """owner/manager/box_office -- orders list & detail."""

    role_check = staticmethod(lambda m: m.can_sell_tickets())


class ScannerRequiredMixin(TenantStaffRequiredMixin):
    """Any role can work the door (Membership.can_scan() is cumulative down
    to 'scanner') -- door scanning UI + redeem endpoint."""

    role_check = staticmethod(lambda m: m.can_scan())
