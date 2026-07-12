"""The three HTTP legs of the OAuth dance.

    start   (tenant host)   -> redirect the user out to the provider
    callback(platform apex) -> provider comes back here; verify + resolve
    complete(tenant host)   -> establish the session on the originating tenant

The pivot through the apex is forced by OAuth registering fixed redirect URIs
against wildcard tenant subdomains (see oauth.service.callback_base_url).
`start` and `complete` are tenant-scoped (@require_tenant); `callback` is NOT
-- it deliberately runs on the platform host where request.organization is
None, and re-derives the tenant from the signed state instead.
"""

import logging
from urllib.parse import urlencode

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login as auth_login
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_GET

from accounts.models import Membership, User
from guests import services as guest_services
from guests.models import GuestAccount
from tenants.decorators import require_tenant
from tenants.models import Organization

from . import service
from . import state as state_mod
from .client import OAuthError
from .providers import get_provider

logger = logging.getLogger(__name__)

# Where each audience lands on failure, and the message shown there. Errors
# ride back to the tenant as ?oauth_error=<code> because the apex callback
# can't flash a message onto the tenant's (separate-host) session; the
# destination view renders the code (see accounts.views / guests.views).
_ERROR_DESTINATION = {service.STAFF: "login", service.GUEST: "guest_portal"}


def _require_provider(name):
    provider = get_provider(name)
    if provider is None:
        # Unknown or unconfigured (no client credentials) -> not a real route.
        raise Http404("Unknown or disabled sign-in provider.")
    return provider


@require_tenant
@require_GET
def oauth_start(request, provider):
    """Begin sign-in: stash a browser-bound nonce and redirect out to the
    provider's consent screen. `audience` (?audience=staff|guest) records
    whether this is staff or buyer sign-in so the callback resolves the right
    kind of account."""
    prov = _require_provider(provider)

    audience = request.GET.get("audience")
    if audience not in service.AUDIENCES:
        raise Http404("Unknown sign-in audience.")

    next_url = request.GET.get("next", "")
    nonce = state_mod.new_nonce()
    authorize_url = service.build_authorize_url(
        request,
        prov,
        audience=audience,
        org_id=request.organization.pk,
        next_url=next_url,
        nonce=nonce,
    )

    response = redirect(authorize_url)
    # The nonce cookie is what binds this flow to THIS browser: the callback
    # only proceeds if the cookie matches the nonce inside the signed state,
    # which is what defeats OAuth login-CSRF. Lax (not Strict) so it's still
    # sent on the top-level GET redirect back from the provider; scoped to the
    # parent domain so it's readable on the apex callback too.
    response.set_cookie(
        state_mod.NONCE_COOKIE,
        nonce,
        max_age=state_mod.STATE_MAX_AGE_SECONDS,
        domain=state_mod.nonce_cookie_domain(),
        secure=request.is_secure(),
        httponly=True,
        samesite="Lax",
    )
    return response


@require_GET
def oauth_callback(request, provider):
    """Provider redirect target (runs on the platform apex). Verifies the
    state + nonce, exchanges the code, resolves the identity, and bounces a
    signed completion hand-off back to the originating tenant -- or bounces an
    error code there instead."""
    prov = _require_provider(provider)

    state_data = state_mod.read_state(request.GET.get("state", ""))
    if state_data is None or state_data.get("p") != provider:
        # Can't trust where this came from -> can't safely bounce anywhere.
        return _apex_error(request, "state")

    # Bind to the browser that started the flow.
    cookie_nonce = request.COOKIES.get(state_mod.NONCE_COOKIE)
    nonce_ok = bool(cookie_nonce) and cookie_nonce == state_data.get("n")

    # From here we know where to send the user back, so failures redirect to
    # the tenant with an error code rather than dead-ending on the apex.
    def back(error=None, token=None):
        return _clear_nonce(_bounce_to_tenant(state_data, error=error, token=token))

    if not nonce_ok:
        return back(error="state")

    # The user declined at the provider's consent screen.
    if request.GET.get("error"):
        return back(error="denied")

    code = request.GET.get("code")
    if not code:
        return back(error="failed")

    organization = Organization.objects.filter(
        pk=state_data.get("org"), is_active=True
    ).first()
    if organization is None:
        return back(error="failed")

    try:
        profile = service.fetch_profile(request, prov, code)
    except OAuthError:
        logger.exception("OAuth token/userinfo exchange failed for %s.", provider)
        return back(error="failed")

    audience = state_data.get("a")
    if audience == service.STAFF:
        resolution = service.resolve_staff(profile, organization)
    elif audience == service.GUEST:
        resolution = service.resolve_guest(profile, organization)
    else:
        return back(error="failed")

    if not resolution.ok:
        return back(error=resolution.error)

    completion = state_mod.make_completion(
        audience=audience,
        org_id=organization.pk,
        account_id=resolution.account.pk,
        next_url=state_data.get("next", ""),
    )
    return back(token=completion)


@require_tenant
@require_GET
def oauth_complete(request, provider=None):
    """Redeem the completion hand-off on the tenant host and actually start
    the session. Re-checks the account against THIS tenant (never trusting the
    token's org alone) before signing anyone in."""
    data = state_mod.read_completion(request.GET.get("token", ""))
    if data is None or str(data.get("org")) != str(request.organization.pk):
        messages.error(request, "That sign-in couldn't be completed. Please try again.")
        return redirect("guest_portal" if data and data.get("a") == service.GUEST else "login")

    audience = data.get("a")
    next_url = _safe_next(request, data.get("next"))

    if audience == service.STAFF:
        return _complete_staff(request, data, next_url)
    if audience == service.GUEST:
        return _complete_guest(request, data, next_url)

    messages.error(request, "That sign-in couldn't be completed. Please try again.")
    return redirect("login")


def _complete_staff(request, data, next_url):
    user = User.objects.filter(pk=data.get("uid"), is_active=True).first()
    # Re-check Membership against the live tenant -- the token proves identity,
    # not access, and access is always decided per-request (accounts.permissions).
    if user is None or not Membership.objects.filter(
        user=user, organization=request.organization
    ).exists():
        return redirect(f"{reverse('login')}?oauth_error=no_access")

    auth_login(request, user, backend="django.contrib.auth.backends.ModelBackend")
    messages.success(request, f"Welcome back, {user.get_short_name()}.")
    return redirect(next_url or "dashboard_overview")


def _complete_guest(request, data, next_url):
    guest = (
        GuestAccount.objects.for_organization(request.organization)
        .filter(pk=data.get("uid"))
        .first()
    )
    if guest is None:
        return redirect(f"{reverse('guest_portal')}?oauth_error=failed")

    guest_services.login_guest(request, guest)
    messages.success(request, "You're signed in. Here are your tickets.")
    return redirect(next_url or "guest_portal")


# --- redirect helpers ----------------------------------------------------


def _bounce_to_tenant(state_data, *, error=None, token=None):
    """Redirect back to the tenant the flow started on. On success carries the
    completion token to oauth_complete; on failure carries ?oauth_error= to
    the audience's sign-in page."""
    if token is not None:
        path = reverse("oauth_complete")
        params = {"token": token}
    else:
        dest = _ERROR_DESTINATION.get(state_data.get("a"), "login")
        path = reverse(dest)
        params = {"oauth_error": error or "failed"}

    scheme = "https" if state_data.get("secure") else "http"
    tenant_param = state_data.get("tp")
    if tenant_param:
        params["_tenant"] = tenant_param
    return redirect(f"{scheme}://{state_data['host']}{path}?{urlencode(params)}")


def _apex_error(request, code):
    """Terminal error on the apex when we can't recover a tenant to bounce to
    (bad/expired/forged state). Renders a friendly try-again page."""
    return _clear_nonce(
        render(request, "oauth/error.html", {"error_code": code}, status=400)
    )


def _clear_nonce(response):
    response.delete_cookie(state_mod.NONCE_COOKIE, domain=state_mod.nonce_cookie_domain())
    return response


def _safe_next(request, next_url):
    if next_url and url_has_allowed_host_and_scheme(
        next_url, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return next_url
    return ""
