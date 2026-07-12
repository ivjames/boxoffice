"""Orchestration for the OAuth sign-in flow: turn a provider + a request into
an authorize URL, and turn a returned code into a verified identity resolved
onto a boxoffice account.

This module is the seam between the transport bits (providers/client/state)
and the app's own identity models. Views stay thin: they call in here and get
back either an account to sign in or an error code to show.

Account resolution differs by audience, on purpose:
  * STAFF never self-provision. OAuth authenticates *identity*, but access
    still requires a pre-existing User with a Membership in this tenant
    (invites remain the only way in -- see accounts.invites). A verified
    Google/Facebook login for someone with no account, or no access here, is
    rejected, not auto-created.
  * GUESTS do self-provision: a ticket buyer signing in with Google is the
    "easy signup" path, so we get_or_create the (tenant, email) GuestAccount
    exactly as checkout would -- an email the provider has verified is enough.
"""

import logging

from django.conf import settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import Membership, User
from guests.models import GuestAccount

from . import state as state_mod
from .client import OAuthError, exchange_code, fetch_userinfo
from .models import OAuthIdentity

logger = logging.getLogger(__name__)

# Audience discriminator carried through the flow (state + completion token).
STAFF = "staff"
GUEST = "guest"
AUDIENCES = (STAFF, GUEST)


def callback_base_url(request):
    """The scheme+host the provider must redirect back to -- necessarily a
    single FIXED value (an OAuth app registers exact redirect URIs; wildcard
    tenant subdomains can't each be one), so the whole flow pivots through the
    platform apex.

    Prefer the explicit OAUTH_CALLBACK_BASE_URL setting (what you register with
    Google/Facebook); dev sets it to http://localhost:8000. If unset, derive
    the apex from BASE_DOMAIN, preserving the current scheme -- correct in prod
    where BASE_DOMAIN is the real apex (boxo.show / beta.boxo.show)."""
    override = (getattr(settings, "OAUTH_CALLBACK_BASE_URL", "") or "").rstrip("/")
    if override:
        return override
    scheme = "https" if request.is_secure() else "http"
    return f"{scheme}://{settings.BASE_DOMAIN}"


def redirect_uri_for(request, provider):
    """The absolute redirect_uri for `provider`'s callback. Built identically
    at authorize-time and token-exchange-time (both go through
    callback_base_url) because the two MUST match byte-for-byte or the
    provider rejects the exchange."""
    return f"{callback_base_url(request)}{reverse('oauth_callback', args=[provider.name])}"


def build_authorize_url(request, provider, *, audience, org_id, next_url, nonce):
    """The provider consent-screen URL to redirect the user to, carrying a
    signed state that round-trips our flow context (see oauth.state)."""
    return_host = request.get_host()
    # Preserve dev's ?_tenant override so the apex->tenant bounce lands on the
    # right tenant back on localhost (prod uses the real subdomain host).
    tenant_param = ""
    if settings.DEBUG:
        tenant_param = request.GET.get("_tenant") or request.headers.get("X-Tenant") or ""

    state = state_mod.make_state(
        provider=provider.name,
        audience=audience,
        org_id=org_id,
        return_host=return_host,
        secure=request.is_secure(),
        tenant_param=tenant_param,
        next_url=next_url,
        nonce=nonce,
    )
    from urllib.parse import urlencode

    params = {
        "client_id": provider.client_id,
        "redirect_uri": redirect_uri_for(request, provider),
        "response_type": "code",
        "scope": provider.scope,
        "state": state,
        # Ask Google to always return a refreshable, consent-confirmed login
        # rather than silently reusing a stale session; harmless to Facebook
        # (it ignores unknown params).
        "prompt": "select_account",
    }
    return f"{provider.authorize_url}?{urlencode(params)}"


def fetch_profile(request, provider, code):
    """Exchange `code` and fetch the userinfo, returning a normalized
    OAuthProfile. Raises client.OAuthError on any transport/provider failure."""
    token = exchange_code(
        provider, code=code, redirect_uri=redirect_uri_for(request, provider)
    )
    userinfo = fetch_userinfo(provider, token["access_token"])
    return provider.normalize(userinfo)


# --- account resolution --------------------------------------------------


class Resolution:
    """Result of mapping a verified provider profile onto a boxoffice account.
    `account` is a User (staff) or GuestAccount (guest) on success; otherwise
    `error` is one of the codes the sign-in pages know how to render."""

    def __init__(self, account=None, error=None):
        self.account = account
        self.error = error

    @property
    def ok(self):
        return self.account is not None


def resolve_staff(profile, organization):
    """Map a verified profile onto a staff User that may sign into this
    tenant, or an error code. Never creates a User (staff join by invite only,
    see accounts.invites): the strongest an OAuth login can do is authenticate
    an already-invited account and, as a convenience, let an invited-but-never-
    set-a-password teammate in without a password."""
    if not profile.email or not profile.email_verified:
        return Resolution(error="no_email")

    # (provider, uid) is the stable link; fall back to the verified email for
    # a first-time OAuth login on an existing (invited) account.
    identity = OAuthIdentity.objects.filter(
        provider=profile.provider, uid=profile.uid
    ).select_related("user").first()
    user = identity.user if identity else User.objects.filter(email__iexact=profile.email).first()

    if user is None:
        return Resolution(error="no_account")
    if not user.is_active:
        return Resolution(error="inactive")
    if not Membership.objects.filter(user=user, organization=organization).exists():
        return Resolution(error="no_access")

    _link_identity(user, profile)
    return Resolution(account=user)


def resolve_guest(profile, organization):
    """Map a verified profile onto this tenant's GuestAccount for that email,
    creating it if new -- OAuth is a first-class signup path for buyers. A
    provider login with no verified email can't be a guest identity."""
    if not profile.email or not profile.email_verified:
        return Resolution(error="no_email")

    guest, _ = GuestAccount.objects.get_or_create_for_email(
        organization, profile.email, name=profile.name
    )
    if guest is None:  # defensive: normalize_email rejected it
        return Resolution(error="no_email")
    return Resolution(account=guest)


def _link_identity(user, profile):
    """Record (or refresh) the user's link to this external identity, so the
    next login matches on the stable provider uid even if their email drifts."""
    OAuthIdentity.objects.update_or_create(
        provider=profile.provider,
        uid=profile.uid,
        defaults={
            "user": user,
            "email": profile.email,
            "last_login_at": timezone.now(),
        },
    )
