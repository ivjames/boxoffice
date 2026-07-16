"""Dependency-free, cache-backed login rate limiting (BO-9).

Blunts password-guessing on staff login and email-bombing on the guest
magic-link request, without pulling in django-axes or a DB table. A fixed
window per (scope, client IP): after settings.LOGIN_RATELIMIT_MAX_ATTEMPTS
failures inside settings.LOGIN_RATELIMIT_WINDOW_SECONDS, is_locked_out()
returns True until the window's cache entry expires. Effectiveness depends on
a cache shared across gunicorn workers -- see config/settings/prod.py, which
configures a file-based cache for this; the dev/test default LocMemCache is
per-process, which is fine there.

Set LOGIN_RATELIMIT_MAX_ATTEMPTS = 0 to disable.
"""

from django.conf import settings
from django.core.cache import cache


def _enabled():
    return settings.LOGIN_RATELIMIT_MAX_ATTEMPTS > 0


def _client_ip(request):
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded:
        # First hop is the client; the rest are proxies (nginx here).
        return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR") or "unknown"


def _key(scope, request):
    return f"loginthrottle:{scope}:{_client_ip(request)}"


def is_locked_out(scope, request):
    """True if this IP has already hit the failure cap for `scope`."""
    if not _enabled():
        return False
    count = cache.get(_key(scope, request), 0)
    return count >= settings.LOGIN_RATELIMIT_MAX_ATTEMPTS


def register_failure(scope, request):
    """Record one failed attempt for this IP/scope and return the running
    count. The first failure sets the fixed-window TTL; later ones increment
    within it (they do NOT extend the window)."""
    if not _enabled():
        return 0
    key = _key(scope, request)
    try:
        return cache.incr(key)
    except ValueError:
        # Key absent (or expired): start a fresh window.
        cache.set(key, 1, timeout=settings.LOGIN_RATELIMIT_WINDOW_SECONDS)
        return 1


def clear(scope, request):
    """Reset the counter for this IP/scope -- called on a successful login so
    a legitimate user isn't penalized for earlier typos."""
    if not _enabled():
        return
    cache.delete(_key(scope, request))


def over_limit(bucket, identity, max_hits, window_seconds):
    """Generic fixed-window limiter: record one hit for (bucket, identity) and
    return True once it has exceeded `max_hits` within `window_seconds`. The
    first hit sets the window's TTL; later ones increment within it.

    Unlike the login helpers above -- which split is_locked_out/register_failure
    so only FAILURES count -- this counts EVERY call, for endpoints where each
    call is itself the cost to bound (e.g. the branding derive agent: an
    external fetch + optional headless render + a Claude request). `identity` is
    whatever the caller wants the window keyed on (an org id, a client IP, …).
    `max_hits <= 0` disables the limit. Shares the same cache caveat as above --
    only effective across workers with a shared cache (prod's file cache)."""
    if max_hits <= 0:
        return False
    key = f"ratelimit:{bucket}:{identity}"
    try:
        count = cache.incr(key)
    except ValueError:
        cache.set(key, 1, timeout=window_seconds)
        count = 1
    return count > max_hits
