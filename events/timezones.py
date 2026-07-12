"""Rendering datetimes in a performance's *venue* timezone.

Times are stored UTC-aware. A showtime is a fact about the place it happens:
"8:00 PM at the Roxy" is 8pm in that venue's own zone for every viewer, and a
touring / second-space org can run venues in different zones (Venue.timezone,
not Organization.timezone). So performance times render in the venue's zone --
NOT the request-level org zone that TenantMiddleware activates as the default
for everything else (order timestamps, hold expiries).
"""
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.utils import timezone


def in_venue_tz(value, tz_name):
    """Return the aware datetime `value` converted to IANA zone `tz_name` for
    display. Falls back to the active zone when the datetime is naive, or when
    `tz_name` is blank or not a real zone -- the timezone field is validated in
    the admin now, but data can predate that or arrive via the CLI, and a bad
    zone must never 500 a ticket page."""
    if value is None:
        return None
    if not timezone.is_aware(value):
        return value
    if tz_name:
        try:
            return timezone.localtime(value, ZoneInfo(tz_name))
        except (ZoneInfoNotFoundError, ValueError):
            pass
    return timezone.localtime(value)
