"""`{% showtime dt tz_name "fmt" %}` — render a performance datetime in its
venue's local timezone.

Use in place of `{{ dt|date:"fmt" }}` for performance/showtime values so each
performance renders in its Venue.timezone rather than the request-level org
zone (see events.timezones.in_venue_tz). Example:

    {% load showtimes %}
    {% showtime perf.starts_at perf.venue.timezone "D, M j Y — g:i A" %}

Calling Django's `date` filter directly (rather than via `{{ …|date }}`) is
deliberate: it bypasses the template engine's automatic re-localization to the
active zone, so the venue-zone value we pass is the value that's formatted.
"""
from django import template
from django.template.defaultfilters import date as date_filter

from ..timezones import in_venue_tz

register = template.Library()

DEFAULT_FORMAT = "D, M j Y — g:i A"


@register.simple_tag
def showtime(value, tz_name, fmt=DEFAULT_FORMAT):
    if value is None:
        return ""
    return date_filter(in_venue_tz(value, tz_name), fmt)
