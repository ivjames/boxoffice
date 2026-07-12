"""Signed, short-lived tokens that carry the OAuth flow safely across hosts.

Why this exists: boxoffice is multi-tenant on wildcard subdomains
(roxy.boxo.show, the-strand.boxo.show, ...), but an OAuth app can only
register a *fixed* redirect URI. So the whole flow pivots through the
platform apex (boxo.show): the tenant page starts the flow, the provider
redirects back to the apex callback, and the apex bounces the finished login
back to the originating tenant. Two tokens make that safe:

  * the **state** token travels out to the provider and back. It records
    where the flow started (tenant org + host) and what it's for (staff vs
    guest sign-in), signed so the callback can trust it wasn't tampered with,
    and carries a nonce we also drop in a browser cookie -- matching the two
    at the callback is what stops OAuth login-CSRF (an attacker can't forge a
    state bound to the victim's browser).

  * the **completion** token is minted only after the provider has vouched
    for the identity, and carries the resolved account across the final
    apex -> tenant redirect, where the tenant host actually establishes the
    session. It's tiny-lived because it's redeemed immediately.

Both are django.core.signing tokens (HMAC on SECRET_KEY, distinct salts),
mirroring guests.tokens' magic-link scheme -- nothing here is readable or
forgeable without the server key.
"""

import secrets

from django.conf import settings
from django.core import signing

_STATE_SALT = "oauth.state"
_COMPLETE_SALT = "oauth.complete"

# The state round-trips through the provider's consent screen, so it has to
# outlive a human pausing on "which account?" -- but not by much.
STATE_MAX_AGE_SECONDS = 15 * 60
# The completion token is redeemed on the very next redirect; keep the replay
# window tiny.
COMPLETE_MAX_AGE_SECONDS = 2 * 60

# Name of the browser cookie holding the state nonce. Set at flow start,
# checked (and cleared) at the callback.
NONCE_COOKIE = "oauth_nonce"


def new_nonce():
    return secrets.token_urlsafe(16)


def make_state(*, provider, audience, org_id, return_host, secure, tenant_param, next_url, nonce):
    """Signed state for the authorize redirect. `audience` is "staff" or
    "guest"; `return_host`/`secure`/`tenant_param` capture exactly where and
    how to bounce the finished login back (tenant_param carries dev's
    ?_tenant= override so localhost flows survive the round-trip)."""
    return signing.dumps(
        {
            "p": provider,
            "a": audience,
            "org": org_id,
            "host": return_host,
            "secure": bool(secure),
            "tp": tenant_param or "",
            "next": next_url or "",
            "n": nonce,
        },
        salt=_STATE_SALT,
    )


def read_state(state, *, max_age=STATE_MAX_AGE_SECONDS):
    """The decoded state dict, or None if the signature is bad, it expired, or
    it's malformed -- every failure mode collapses to "restart the flow"."""
    try:
        data = signing.loads(state, salt=_STATE_SALT, max_age=max_age)
    except signing.BadSignature:
        return None
    if not isinstance(data, dict):
        return None
    return data


def make_completion(*, audience, org_id, account_id, next_url):
    """Signed hand-off token: the identity is already verified; this just
    carries which account to sign in on the final tenant redirect."""
    return signing.dumps(
        {"a": audience, "org": org_id, "uid": account_id, "next": next_url or ""},
        salt=_COMPLETE_SALT,
    )


def read_completion(token, *, max_age=COMPLETE_MAX_AGE_SECONDS):
    try:
        data = signing.loads(token, salt=_COMPLETE_SALT, max_age=max_age)
    except signing.BadSignature:
        return None
    if not isinstance(data, dict):
        return None
    return data


def nonce_cookie_domain():
    """Domain to scope the nonce cookie to so it's readable on BOTH the tenant
    subdomain (where the flow starts) and the platform apex (where the
    callback lands): the parent zone, e.g. ".boxo.show". In dev BASE_DOMAIN is
    "localhost" (start and callback are the same host), so a host-only cookie
    is correct -- return None there. An apex without a dot is likewise
    host-only."""
    base = settings.BASE_DOMAIN
    if base and base != "localhost" and "." in base:
        return "." + base
    return None
