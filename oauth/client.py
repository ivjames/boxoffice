"""Minimal OAuth2 HTTP calls, on the standard library only.

Deliberately no `requests`/`httpx` dependency: the two calls we make (exchange
an authorization code for an access token, then GET the userinfo endpoint) are
small enough that urllib keeps the dependency list -- which this repo keeps
tight and justified, see requirements.txt -- unchanged. Every call is bounded
by a timeout so a hung provider can't wedge a gunicorn worker, and transport/
protocol failures surface as OAuthError for the view to turn into a friendly
"couldn't sign you in" rather than a 500.
"""

import json
import urllib.error
import urllib.parse
import urllib.request

# Providers should answer in well under this; the cap just stops a stalled
# connection from pinning a worker indefinitely.
_TIMEOUT = 10


class OAuthError(Exception):
    """A token exchange or userinfo fetch failed (network, non-200, or a
    provider-reported error). The message is for logs, not end users."""


def _read_json(request):
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT) as resp:
            body = resp.read()
    except urllib.error.HTTPError as exc:
        # Surface the provider's error body in the log message -- it's usually
        # the actionable part (bad redirect_uri, expired code, etc.).
        detail = exc.read().decode("utf-8", "replace")[:500]
        raise OAuthError(f"HTTP {exc.code} from provider: {detail}") from exc
    except urllib.error.URLError as exc:
        raise OAuthError(f"Could not reach provider: {exc.reason}") from exc

    try:
        return json.loads(body)
    except ValueError as exc:
        raise OAuthError("Provider returned a non-JSON response.") from exc


def exchange_code(provider, *, code, redirect_uri):
    """Trade an authorization `code` for an access token at the provider's
    token endpoint. Returns the parsed token response (dict); raises
    OAuthError on any failure or if no access_token comes back."""
    data = urllib.parse.urlencode(
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": provider.client_id,
            "client_secret": provider.client_secret,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        provider.token_url,
        data=data,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    payload = _read_json(request)
    if not payload.get("access_token"):
        raise OAuthError("Token response carried no access_token.")
    return payload


def fetch_userinfo(provider, access_token):
    """GET the provider's userinfo/profile endpoint with `access_token`.
    Returns the raw profile JSON (dict) for the provider's normalize() to
    map; raises OAuthError on failure."""
    params = dict(provider.userinfo_params or {})
    url = provider.userinfo_url
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"}
    )
    return _read_json(request)
