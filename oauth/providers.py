"""The set of OAuth providers boxoffice can sign a user in with, and how each
one's authorize/token/userinfo dance is shaped.

Each Provider is a small, declarative description of one identity provider:
its OAuth2 endpoints, the scopes we ask for, and -- crucially -- a
`normalize(userinfo)` that maps that provider's own JSON profile shape onto
the single OAuthProfile shape the rest of the app consumes (oauth.service).
Adding a provider is adding one entry here plus a pair of client-credential
settings; nothing downstream needs to know Google's shape from Facebook's.

A provider is *enabled* only when its client id + secret are configured (env
-> settings); `enabled_providers()` is what the sign-in templates iterate, so
a provider with no credentials simply never renders a button and its
start/callback URLs 404. That means shipping this code with no secrets set is
inert and safe -- nothing turns on until an operator supplies real keys.
"""

from dataclasses import dataclass
from typing import Callable, Optional

from django.conf import settings


@dataclass(frozen=True)
class OAuthProfile:
    """The provider-agnostic identity we resolve a login against.

    `uid` is the provider's own stable, opaque subject id (Google `sub`,
    Facebook `id`) -- it never changes even if the person renames their
    account or email, so it's the primary key we link staff accounts on.
    `email_verified` gates whether we'll trust `email` as an identity: we
    only ever match/create an account on an email the provider vouches the
    user actually controls, so a provider account can't claim someone else's
    boxoffice email."""

    provider: str
    uid: str
    email: str
    email_verified: bool
    name: str = ""
    first_name: str = ""
    last_name: str = ""


@dataclass(frozen=True)
class Provider:
    name: str
    label: str
    authorize_url: str
    token_url: str
    userinfo_url: str
    scope: str
    # Maps the provider's raw userinfo JSON -> OAuthProfile. Kept as a plain
    # callable so each provider owns the quirks of its own payload.
    normalize: Callable[[dict], OAuthProfile]
    # Extra params some providers want on the userinfo GET (Facebook needs an
    # explicit ?fields= list or it returns almost nothing).
    userinfo_params: Optional[dict] = None

    @property
    def client_id(self):
        return getattr(settings, f"{self.name.upper()}_OAUTH_CLIENT_ID", "") or ""

    @property
    def client_secret(self):
        return getattr(settings, f"{self.name.upper()}_OAUTH_CLIENT_SECRET", "") or ""

    @property
    def enabled(self):
        return bool(self.client_id and self.client_secret)


def _normalize_google(info: dict) -> OAuthProfile:
    return OAuthProfile(
        provider="google",
        uid=str(info.get("sub", "")),
        email=(info.get("email") or "").strip(),
        # Google's userinfo returns email_verified as a real bool.
        email_verified=bool(info.get("email_verified")),
        name=info.get("name", "") or "",
        first_name=info.get("given_name", "") or "",
        last_name=info.get("family_name", "") or "",
    )


def _normalize_facebook(info: dict) -> OAuthProfile:
    return OAuthProfile(
        provider="facebook",
        uid=str(info.get("id", "")),
        email=(info.get("email") or "").strip(),
        # The Graph API only ever returns an email for a confirmed address, so
        # a present email is a verified one. (No email => the account has none
        # or hasn't confirmed it; service.py treats that as "no usable email".)
        email_verified=bool(info.get("email")),
        name=info.get("name", "") or "",
        first_name=info.get("first_name", "") or "",
        last_name=info.get("last_name", "") or "",
    )


_PROVIDERS = {
    "google": Provider(
        name="google",
        label="Google",
        authorize_url="https://accounts.google.com/o/oauth2/v2/auth",
        token_url="https://oauth2.googleapis.com/token",
        userinfo_url="https://openidconnect.googleapis.com/v1/userinfo",
        scope="openid email profile",
        normalize=_normalize_google,
    ),
    "facebook": Provider(
        name="facebook",
        label="Facebook",
        authorize_url="https://www.facebook.com/v19.0/dialog/oauth",
        token_url="https://graph.facebook.com/v19.0/oauth/access_token",
        userinfo_url="https://graph.facebook.com/v19.0/me",
        scope="email public_profile",
        normalize=_normalize_facebook,
        userinfo_params={"fields": "id,name,email,first_name,last_name"},
    ),
}


def get_provider(name):
    """The Provider named `name` iff it exists AND has credentials configured,
    else None. Callers (views) treat None as 404 -- an unknown or
    unconfigured provider is simply not a route that exists."""
    provider = _PROVIDERS.get(name)
    if provider is None or not provider.enabled:
        return None
    return provider


def enabled_providers():
    """Every configured provider, in a stable order, for the sign-in
    templates to render a button per entry."""
    return [p for p in _PROVIDERS.values() if p.enabled]
