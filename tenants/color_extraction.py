"""The "little agent" that looks at a client's homepage and derives a
six-role color scheme from it (tenants.color_schemes' role model).

Two stages, each independently testable:

1. `extract_candidate_colors(html, base_url, fetch=...)` -- pull every color
   token out of the page: inline `style=""`, `<style>` blocks, and linked
   stylesheets (a bounded few, fetched via the injected `fetch`). Colors are
   parsed from hex, rgb()/rgba(), and the common CSS named colors, then
   weighted by how often they appear (a color used 30 times outranks a
   one-off). Returns `[(hex, weight), ...]`, most-used first.

2. `assign_roles(candidates)` -- deterministically slot the weighted
   candidates into the six roles by luminance and saturation: the most-used
   saturated color becomes `primary`, the next distinct one `secondary`, the
   darkest becomes `dark_accent` / `neutral`, the lightest `light_neutral`, and
   the most saturated warm/gold-ish one `feature_accent`. Always returns all six
   roles (falling back to sensible defaults when the page is monochrome).

`derive_scheme_from_url(url)` ties them together: fetch the page, extract,
assign, and -- ONLY when an Anthropic API key is configured (mirroring
venues.chart_parsing's opt-in) -- ask Claude to name the result and refine the
role assignment. With no key it still returns a complete, usable scheme from
the deterministic pass; the network fetch is the only hard dependency.

Nothing here writes to the database. The dashboard view decides whether to
apply the derived colors or save them as a ColorScheme.
"""

import logging
import re
from collections import defaultdict
from urllib.parse import urljoin, urlparse

from django.conf import settings

from .color_schemes import ROLE_KEYS

logger = logging.getLogger(__name__)

# Don't chase a whole site -- the homepage plus a handful of its stylesheets is
# plenty to read a brand's colors off, and bounds both time and bytes.
MAX_STYLESHEETS = 5
MAX_FETCH_BYTES = 2 * 1024 * 1024
FETCH_TIMEOUT = 10

# The subset of CSS named colors worth recognizing -- the ones brands actually
# write by name. Anything exotic is far likelier to appear as a hex.
_NAMED_COLORS = {
    "black": "#000000", "white": "#ffffff", "red": "#ff0000", "green": "#008000",
    "blue": "#0000ff", "navy": "#000080", "teal": "#008080", "purple": "#800080",
    "maroon": "#800000", "olive": "#808000", "gold": "#ffd700", "silver": "#c0c0c0",
    "gray": "#808080", "grey": "#808080", "orange": "#ffa500", "pink": "#ffc0cb",
    "crimson": "#dc143c", "indigo": "#4b0082", "ivory": "#fffff0", "beige": "#f5f5dc",
}

_HEX_RE = re.compile(r"#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})\b")
_RGB_RE = re.compile(r"rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)", re.IGNORECASE)
# Named colors only in a CSS *value* position: right after a `:` (optionally
# with whitespace), as a whole token. This deliberately misses `border: 1px
# solid navy` but avoids the far worse false positives -- class selectors like
# `.gold`, ids like `#navy`, and English words in body text ("Golden Age").
_NAMED_RE = re.compile(
    r":\s*(" + "|".join(sorted(_NAMED_COLORS, key=len, reverse=True)) + r")(?![\w-])",
    re.IGNORECASE,
)
_LINK_CSS_RE = re.compile(
    r"""<link\b[^>]*\brel\s*=\s*["']?[^"'>]*stylesheet[^"'>]*["']?[^>]*>""",
    re.IGNORECASE,
)
_HREF_RE = re.compile(r"""\bhref\s*=\s*["']([^"']+)["']""", re.IGNORECASE)
_STYLE_BLOCK_RE = re.compile(r"<style\b[^>]*>(.*?)</style>", re.IGNORECASE | re.DOTALL)


class ColorDeriveError(Exception):
    """Raised when the homepage can't be fetched/read. Message is safe to show
    directly to dashboard staff."""


# --- color math -----------------------------------------------------------


def _normalize_hex(value):
    """Canonical lowercase #rrggbb, expanding #rgb shorthand. None if unparsable."""
    value = value.strip().lower()
    if not value.startswith("#"):
        return None
    body = value[1:]
    if len(body) == 3:
        body = "".join(ch * 2 for ch in body)
    if len(body) != 6 or any(c not in "0123456789abcdef" for c in body):
        return None
    return "#" + body


def _rgb(hex_color):
    body = hex_color[1:]
    return tuple(int(body[i : i + 2], 16) for i in (0, 2, 4))


def _luminance(hex_color):
    """Perceived brightness 0..1 (Rec. 601 luma)."""
    r, g, b = _rgb(hex_color)
    return (0.299 * r + 0.587 * g + 0.114 * b) / 255.0


def _saturation(hex_color):
    """HSL saturation 0..1 -- how far from gray. Near-0 for blacks/whites/grays."""
    r, g, b = (c / 255.0 for c in _rgb(hex_color))
    hi, lo = max(r, g, b), min(r, g, b)
    if hi == lo:
        return 0.0
    delta = hi - lo
    mid = (hi + lo) / 2
    return delta / (2 - hi - lo) if mid > 0.5 else delta / (hi + lo)


def _warmth(hex_color):
    """Rough gold/brass affinity: reward red+green (yellow) over blue. Used to
    pick the `feature_accent` role, which reads best as a warm highlight."""
    r, g, b = _rgb(hex_color)
    return (r + g) / 2 - b


# --- stage 1: extract candidate colors ------------------------------------


def _colors_in(text):
    """Every color token in a blob of HTML/CSS, normalized to #rrggbb, in
    appearance order (with repeats -- repetition is the weight signal)."""
    found = []
    for m in _HEX_RE.finditer(text):
        hex_color = _normalize_hex(m.group(0))
        if hex_color:
            found.append(hex_color)
    for m in _RGB_RE.finditer(text):
        r, g, b = (min(255, int(m.group(i))) for i in (1, 2, 3))
        found.append("#%02x%02x%02x" % (r, g, b))
    for m in _NAMED_RE.finditer(text):
        found.append(_NAMED_COLORS[m.group(1).lower()])
    return found


def extract_candidate_colors(html, base_url="", fetch=None):
    """Weighted candidate colors from a page and its stylesheets, most-used
    first. `fetch(url) -> str` retrieves a linked stylesheet's text (injected
    so callers/tests control the network); stylesheet fetch failures are
    skipped, not fatal -- the inline colors still count."""
    weights = defaultdict(int)

    def tally(text):
        for hex_color in _colors_in(text):
            weights[hex_color] += 1

    tally(html)
    for block in _STYLE_BLOCK_RE.findall(html):
        tally(block)

    if fetch is not None and base_url:
        sheet_urls = []
        for link in _LINK_CSS_RE.findall(html):
            href_match = _HREF_RE.search(link)
            if href_match:
                sheet_urls.append(urljoin(base_url, href_match.group(1)))
        for sheet_url in sheet_urls[:MAX_STYLESHEETS]:
            try:
                tally(fetch(sheet_url))
            except Exception:  # noqa: BLE001 -- a bad stylesheet is not fatal
                logger.debug("Skipped stylesheet %s", sheet_url, exc_info=True)

    # Pure #000000 / #ffffff dominate almost every page's CSS reset and carry
    # no brand signal on their own; keep them only as neutral fallbacks, never
    # let them outrank a real brand color.
    return sorted(weights.items(), key=lambda kv: (-kv[1], kv[0]))


# --- stage 2: assign roles ------------------------------------------------

_FALLBACK = {
    "primary": "#1e293b",
    "secondary": "#475569",
    "dark_accent": "#0f172a",
    "feature_accent": "#c9a227",
    "light_neutral": "#f1f5f9",
    "neutral": "#111111",
}


def assign_roles(candidates):
    """Slot weighted candidates (from extract_candidate_colors) into the six
    roles by luminance/saturation/warmth. Always returns every role; leans on
    _FALLBACK for any role the page gives nothing usable for."""
    roles = dict(_FALLBACK)
    if not candidates:
        return roles

    colors = [c for c, _w in candidates]
    saturated = [c for c in colors if _saturation(c) >= 0.2]

    # Primary + secondary: the two most-used *saturated* brand colors (fall
    # back to most-used overall when the page is largely gray).
    brand_ranked = saturated or colors
    roles["primary"] = brand_ranked[0]
    roles["secondary"] = next(
        (c for c in brand_ranked[1:] if c != roles["primary"]), roles["primary"]
    )

    # Darkest color anchors both the dark accent and the near-black neutral;
    # lightest anchors the light neutral. Judge across everything seen.
    roles["dark_accent"] = min(colors, key=_luminance)
    roles["neutral"] = min(colors, key=_luminance)
    roles["light_neutral"] = max(colors, key=_luminance)

    # Feature accent: the warmest reasonably-saturated color (gold/brass/copper
    # read best as the highlight). Keep the default gold if nothing warm shows.
    warm = [c for c in colors if _saturation(c) >= 0.2 and _warmth(c) > 20]
    if warm:
        roles["feature_accent"] = max(warm, key=_warmth)

    return roles


# --- stage 3: fetch + (optional) Claude refinement ------------------------


def _http_fetch(url):
    """Fetch a URL's text via `requests`, capped at MAX_FETCH_BYTES. Outbound
    HTTPS goes through the environment's configured proxy automatically
    (requests honors HTTPS_PROXY)."""
    import requests

    resp = requests.get(
        url,
        timeout=FETCH_TIMEOUT,
        headers={"User-Agent": "boxo.show color-scheme agent"},
        stream=True,
    )
    resp.raise_for_status()
    content = resp.raw.read(MAX_FETCH_BYTES, decode_content=True) or b""
    return content.decode(resp.encoding or "utf-8", errors="replace")


def _default_name(url):
    host = (urlparse(url).hostname or "").replace("www.", "")
    label = host.split(".")[0].replace("-", " ").title() if host else "Homepage"
    return f"{label} palette"


def derive_scheme_from_url(url, *, fetch=None):
    """Fetch `url`, extract its colors, and return a derived scheme dict:
    `{"name": str, "roles": {role: hex, ...}, "source_url": url,
    "candidates": [(hex, weight), ...]}`.

    `fetch` (injectable for tests) retrieves page/stylesheet text; defaults to
    a real `requests` GET through the environment proxy. When an Anthropic API
    key is configured the raw assignment is handed to Claude for naming and
    refinement (see _refine_with_claude); otherwise the deterministic result
    is returned as-is."""
    if not re.match(r"^https?://", url, re.IGNORECASE):
        url = "https://" + url
    fetch = fetch or _http_fetch

    try:
        html = fetch(url)
    except Exception as exc:  # noqa: BLE001 -- normalize every fetch failure
        raise ColorDeriveError(
            f"Couldn't load {url}. Check the address and that the site is reachable."
        ) from exc

    candidates = extract_candidate_colors(html, base_url=url, fetch=fetch)
    roles = assign_roles(candidates)
    scheme = {
        "name": _default_name(url),
        "roles": roles,
        "source_url": url,
        "candidates": candidates[:24],
    }

    if getattr(settings, "ANTHROPIC_API_KEY", ""):
        try:
            _refine_with_claude(scheme)
        except Exception:  # noqa: BLE001 -- refinement is best-effort
            logger.warning("Claude scheme refinement failed; using heuristic result", exc_info=True)
    return scheme


_REFINE_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "A short, evocative name for this palette, e.g. 'Royal Amethyst'."},
        **{
            role: {"type": "string", "description": f"The {role} role color as #rrggbb hex."}
            for role in ROLE_KEYS
        },
    },
    "required": ["name", *ROLE_KEYS],
    "additionalProperties": False,
}


def _refine_with_claude(scheme):
    """In-place: ask Claude to name the palette and refine the six role
    assignments from the extracted candidates. Mirrors venues.chart_parsing's
    opt-in Claude usage (key-gated, structured output). Best-effort -- the
    caller swallows failures and keeps the deterministic result."""
    import json

    import anthropic

    client = anthropic.Anthropic(api_key=getattr(settings, "ANTHROPIC_API_KEY", "") or None)
    model = getattr(settings, "CHART_PARSING_MODEL", "claude-opus-4-8")
    swatches = ", ".join(f"{c} (used {w}x)" for c, w in scheme["candidates"])
    prompt = (
        "You are a brand designer. From these colors extracted from a theater's "
        f"homepage ({scheme['source_url']}), choose a cohesive six-role palette.\n\n"
        f"Colors, most-used first: {swatches}\n\n"
        f"A first-pass heuristic proposed: {json.dumps(scheme['roles'])}\n\n"
        "Refine into the six roles: primary (main brand), secondary (supporting), "
        "dark_accent (deep shade), feature_accent (warm highlight/CTA), light_neutral "
        "(light background), neutral (near-black text). Prefer colors actually "
        "present on the page; only invent a color if a role has no good match. "
        "Give a short evocative name."
    )
    response = client.messages.create(
        model=model,
        max_tokens=1024,
        output_config={"format": {"type": "json_schema", "schema": _REFINE_SCHEMA}},
        messages=[{"role": "user", "content": prompt}],
    )
    if response.stop_reason == "refusal":
        return
    text = "".join(block.text for block in response.content if getattr(block, "type", None) == "text")
    data = json.loads(text)
    scheme["name"] = data.get("name") or scheme["name"]
    for role in ROLE_KEYS:
        candidate = _normalize_hex(str(data.get(role, "")))
        if candidate:
            scheme["roles"][role] = candidate
