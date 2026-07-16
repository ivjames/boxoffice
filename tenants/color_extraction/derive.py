"""Ties the extraction/scoring, network-fetch, screenshot, and vision-refine
pieces together into the single public entry point the dashboard view calls."""

import logging
import re

from django.conf import settings

from ._netfetch import _http_fetch
from ._screenshot import render_homepage_png
from .scoring import ColorDeriveError, _default_name, _extract_weighted, assign_roles

logger = logging.getLogger(__name__)


def derive_scheme_from_url(url, *, fetch=None, render=None):
    """Fetch `url`, extract its colors, and return a derived scheme dict:
    `{"name": str, "roles": {role: hex, ...}, "source_url": url,
    "candidates": [(hex, weight), ...], "context": {hex: label},
    "method": "heuristic"|"vision"|"text"}`.

    `fetch` (injectable for tests) retrieves page/stylesheet text; `render`
    (also injectable) returns a homepage screenshot as PNG bytes, or None. Both
    default to real implementations through the environment proxy.

    Refinement, when an Anthropic API key is configured: if a screenshot renders
    Claude picks the palette from what's VISIBLE (`_derive_with_vision`);
    otherwise it refines the extracted candidates from text alone
    (`_refine_with_claude`). With no key the deterministic result stands."""
    if not re.match(r"^https?://", url, re.IGNORECASE):
        url = "https://" + url
    fetch = fetch or _http_fetch

    try:
        html = fetch(url)
    except Exception as exc:  # noqa: BLE001 -- normalize every fetch failure
        raise ColorDeriveError(
            f"Couldn't load {url}. Check the address and that the site is reachable."
        ) from exc

    candidates, context = _extract_weighted(html, base_url=url, fetch=fetch)
    roles = assign_roles(candidates)
    scheme = {
        "name": _default_name(url),
        "roles": roles,
        "source_url": url,
        "candidates": candidates[:24],
        "context": context,
        "method": "heuristic",
    }

    if getattr(settings, "ANTHROPIC_API_KEY", ""):
        render = render or render_homepage_png
        screenshot = None
        try:
            screenshot = render(url)
        except Exception:  # noqa: BLE001 -- a failed render just means no vision
            logger.warning("Homepage render failed for %s; refining from text", url, exc_info=True)
        try:
            # Looked up on the package itself (not imported as bare names) so
            # that tests can `patch.object(tenants.color_extraction, ...)` these
            # two collaborators without touching a browser or the Claude API.
            from tenants import color_extraction as _color_extraction

            if screenshot:
                _color_extraction._derive_with_vision(scheme, screenshot)
            else:
                _color_extraction._refine_with_claude(scheme)
        except Exception:  # noqa: BLE001 -- refinement is best-effort
            logger.warning("Claude scheme refinement failed; using heuristic result", exc_info=True)
    return scheme
