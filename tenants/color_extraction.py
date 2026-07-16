"""The "little agent" that looks at a client's homepage and derives a
six-role color scheme from it (tenants.color_schemes' role model).

Two stages, each independently testable:

1. `extract_candidate_colors(html, base_url, fetch=...)` -- pull every color
   token out of the page: inline `style=""`, `<style>` blocks, and linked
   stylesheets (a bounded few, fetched via the injected `fetch`). Colors are
   parsed from hex, rgb()/rgba(), and the common CSS named colors, then ranked
   by CONTEXT, not raw frequency. Where a color is used decides how much it
   counts: painted surfaces (backgrounds), explicit brand/CTA elements, and
   `--brand`/`--primary` CSS variables are weighted heavily; link/anchor text
   colors are suppressed to zero (they're the framework defaults that used to
   hijack `primary` -- a default link blue is on every link, so by raw count it
   buried the real brand). Returns `[(hex, count), ...]`, strongest brand
   signal first, with `count` an honest occurrence tally for display.

2. `assign_roles(candidates)` -- deterministically slot the ranked candidates
   into the six roles by luminance and saturation: the top-ranked saturated
   color becomes `primary`, the next distinct one `secondary`, the darkest
   becomes `dark_accent` / `neutral`, the lightest `light_neutral`, and the
   most saturated warm/gold-ish one `feature_accent`. Always returns all six
   roles (falling back to sensible defaults when the page is monochrome).

`derive_scheme_from_url(url)` ties them together: fetch the page, extract,
assign, and -- ONLY when an Anthropic API key is configured (mirroring
venues.chart_parsing's opt-in) -- ask Claude to name the result and refine the
role assignment. With no key it still returns a complete, usable scheme from
the deterministic pass; the network fetch is the only hard dependency.

Nothing here writes to the database. The dashboard view decides whether to
apply the derived colors or save them as a ColorScheme.
"""

import ipaddress
import logging
import re
import socket
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
# Named colors are matched only inside an isolated CSS declaration value
# (_NAMED_VALUE_RE, defined with the stage-1 machinery below) -- never against
# raw markup, where `.gold` selectors and prose like "Golden Age" would be
# false positives.
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


# --- stage 1: extract candidate colors (context-aware) --------------------
#
# The old pass counted every color token equally, so a default link blue -- one
# `a { color }` rule that paints every link on the page -- outweighed the brand
# color a header or button uses once. We now score each color by the CSS
# CONTEXT it appears in: a color is a brand signal in proportion to how much of
# the page it *paints* and how deliberately it's named, not how many little
# links reuse it. Ranking is by that score; the tally we return/display stays an
# honest occurrence count.

# Named colors, matched as a whole token inside an already-isolated CSS *value*
# (so no leading-colon anchor needed and no risk of hitting prose -- the value
# is known CSS, not body text).
_NAMED_VALUE_RE = re.compile(
    r"(?<![\w-])(" + "|".join(sorted(_NAMED_COLORS, key=len, reverse=True)) + r")(?![\w-])",
    re.IGNORECASE,
)

# One CSS rule: `selector { declarations }`. Non-greedy, brace-free bodies, so a
# flat scan also picks the inner rules out of `@media { ... }` wrappers.
_RULE_RE = re.compile(r"([^{}]+)\{([^{}]*)\}", re.DOTALL)

# Any element with an inline `style=""` (tag captured so we know when it's a
# link, whose text color we still want to suppress).
_INLINE_STYLE_RE = re.compile(
    r"""<\s*([a-zA-Z][\w-]*)\b[^>]*\bstyle\s*=\s*["']([^"']*)["']""", re.IGNORECASE
)

# --- selector / property classification for scoring ---
#
# Selectors and custom-property names are classified by their WORD TOKENS, split
# on every non-alphanumeric (`.`, `#`, `-`, `_`, whitespace, combinators). That
# way `btn-primary`, `site-logo`, and `--brand-color` all surface their meaning
# token -- a plain word-boundary regex would choke on the hyphens that class and
# variable names lean on everywhere.
_TOKEN_SPLIT_RE = re.compile(r"[^a-z0-9]+")
# Link/anchor pseudo-classes need the `:`, so they stay a regex over the raw
# selector; the standalone `a` element and `.link` classes fall out as tokens.
_PSEUDO_LINK_RE = re.compile(r":(?:link|visited|hover|focus|active)\b", re.IGNORECASE)
_LINK_SEL_TOKENS = {"a", "link", "links", "anchor"}
# Deliberate brand / call-to-action surfaces -- the strongest painted signal.
_BRAND_SEL_TOKENS = {"brand", "logo", "cta", "btn", "button", "primary", "accent"}
# Big chrome surfaces that carry the brand's fill across large areas.
_SURFACE_SEL_TOKENS = {
    "body", "header", "footer", "nav", "navbar", "main", "section", "aside",
    "hero", "banner", "masthead", "jumbotron", "topbar", "cover", "splash",
}
# CSS custom properties a theme names for its brand vs. its links.
_BRAND_VAR_TOKENS = {"brand", "primary", "accent", "theme", "logo", "main"}
_LINK_VAR_TOKENS = {"link", "links", "anchor"}

_BG_PROPS = {"background", "background-color", "background-image"}
_EDGE_PROPS = {
    "border", "border-color", "border-top-color", "border-right-color",
    "border-bottom-color", "border-left-color", "outline", "outline-color",
    "box-shadow", "text-shadow", "text-decoration-color",
}


def _context_score(selector, prop):
    """How strong a brand signal a color is, given the CSS (selector, property)
    it was declared in. Returns `(weight, label)`; weight 0 means "seen, but
    never let this steer a role" (the link-text suppression that fixes a default
    link color winning `primary`). `label` is a human context hint passed on to
    Claude's refinement."""
    sel = (selector or "").strip().lower()
    prop = (prop or "").strip().lower()

    # Custom properties: the theme has *named* the color's job -- trust the name.
    if prop.startswith("--"):
        tokens = set(_TOKEN_SPLIT_RE.split(prop[2:]))
        if tokens & _LINK_VAR_TOKENS:
            return 0, "link variable"
        if tokens & _BRAND_VAR_TOKENS:
            return 12, "brand variable"
        return 3, "variable"

    tokens = set(_TOKEN_SPLIT_RE.split(sel))
    interactive = bool(_PSEUDO_LINK_RE.search(sel)) or bool(tokens & _LINK_SEL_TOKENS)

    if prop == "color":
        # Link/anchor text: the framework-default culprit. Never rank it.
        if interactive:
            return 0, "link"
        return 1, "text"

    if prop in _BG_PROPS:
        if tokens & _BRAND_SEL_TOKENS:
            return 10, "brand surface"
        if tokens & _SURFACE_SEL_TOKENS:
            return 6, "surface"
        if interactive:
            return 2, "interactive surface"
        return 4, "background"

    if prop in _EDGE_PROPS:
        return 1, "accent"

    if prop in ("fill", "stroke"):  # SVG -- often the logo itself
        return 2, "graphic"

    return 1, "other"


def _colors_in_value(value):
    """Every color in an isolated CSS declaration value, normalized to #rrggbb."""
    found = []
    for m in _HEX_RE.finditer(value):
        hex_color = _normalize_hex(m.group(0))
        if hex_color:
            found.append(hex_color)
    for m in _RGB_RE.finditer(value):
        r, g, b = (min(255, int(m.group(i))) for i in (1, 2, 3))
        found.append("#%02x%02x%02x" % (r, g, b))
    for m in _NAMED_VALUE_RE.finditer(value):
        found.append(_NAMED_COLORS[m.group(1).lower()])
    return found


def _hex_rgb_in(text):
    """Bare hex/rgb() colors in a blob of markup (SVG fills, `bgcolor=`, etc.),
    with no CSS context. Named colors are skipped here -- outside a CSS value
    they're far likelier to be English words than colors."""
    found = []
    for m in _HEX_RE.finditer(text):
        hex_color = _normalize_hex(m.group(0))
        if hex_color:
            found.append(hex_color)
    for m in _RGB_RE.finditer(text):
        r, g, b = (min(255, int(m.group(i))) for i in (1, 2, 3))
        found.append("#%02x%02x%02x" % (r, g, b))
    return found


def _tally_css(css_text, score, count, context, *, selector_hint=None):
    """Accumulate one CSS blob into the running score/count/context maps. When
    `selector_hint` is given (an inline style's element) it stands in for the
    selector; otherwise rules are parsed out of `css_text`."""
    def record(hex_color, weight, label):
        count[hex_color] += 1
        score[hex_color] += weight
        # Label the color by the strongest context it was ever seen in.
        best = context.get(hex_color)
        if best is None or weight > best[0]:
            context[hex_color] = (weight, label)

    if selector_hint is not None:
        for decl in css_text.split(";"):
            prop, sep, value = decl.partition(":")
            if not sep:
                continue
            weight, label = _context_score(selector_hint, prop)
            for hex_color in _colors_in_value(value):
                record(hex_color, weight, label)
        return

    for selector, body in _RULE_RE.findall(css_text):
        if selector.lstrip().startswith("@"):  # @font-face/@keyframes: no brand color
            continue
        for decl in body.split(";"):
            prop, sep, value = decl.partition(":")
            if not sep:
                continue
            weight, label = _context_score(selector, prop)
            for hex_color in _colors_in_value(value):
                record(hex_color, weight, label)


def _extract_weighted(html, base_url="", fetch=None):
    """Context-aware core: returns `(ranked, context)` where `ranked` is
    `[(hex, count), ...]` strongest-brand-signal first and `context` maps each
    hex to a short "where it's used" label. `extract_candidate_colors` is the
    thin public wrapper (drops the context); `derive_scheme_from_url` uses the
    context to brief Claude."""
    score = defaultdict(int)
    count = defaultdict(int)
    context = {}

    # 1. <style> blocks -- parsed as CSS (selector-aware).
    for block in _STYLE_BLOCK_RE.findall(html):
        _tally_css(block, score, count, context)

    # 2. Inline style="" -- property context only, but still link-aware via tag.
    html_wo_style = _STYLE_BLOCK_RE.sub(" ", html)
    consumed = []
    for tag, decls in _INLINE_STYLE_RE.findall(html_wo_style):
        _tally_css(decls, score, count, context, selector_hint=tag)
        consumed.append(decls)

    # 3. Everything else in the markup (SVG fills, bgcolor=, stray tokens) at a
    #    low, context-free weight -- present so nothing is lost, never dominant.
    leftover = html_wo_style
    for decls in consumed:
        leftover = leftover.replace(decls, " ", 1)
    for hex_color in _hex_rgb_in(leftover):
        count[hex_color] += 1
        score[hex_color] += 1
        context.setdefault(hex_color, (1, "markup"))

    # 4. Linked stylesheets -- the richest source; parsed as CSS like (1).
    if fetch is not None and base_url:
        sheet_urls = []
        for link in _LINK_CSS_RE.findall(html):
            href_match = _HREF_RE.search(link)
            if href_match:
                sheet_urls.append(urljoin(base_url, href_match.group(1)))
        for sheet_url in sheet_urls[:MAX_STYLESHEETS]:
            try:
                _tally_css(fetch(sheet_url), score, count, context)
            except Exception:  # noqa: BLE001 -- a bad stylesheet is not fatal
                logger.debug("Skipped stylesheet %s", sheet_url, exc_info=True)

    # Rank by brand-signal score (context), break ties by honest count then hex;
    # the returned weight is the count, so display ("used N×") stays truthful.
    ranked = sorted(
        count.items(), key=lambda kv: (-score[kv[0]], -kv[1], kv[0])
    )
    return ranked, {h: label for h, (_w, label) in context.items()}


def extract_candidate_colors(html, base_url="", fetch=None):
    """Candidate colors from a page and its stylesheets, ranked by CONTEXT
    (brand surfaces and named brand variables first; link/anchor text colors
    last), each paired with its honest occurrence count. `fetch(url) -> str`
    retrieves a linked stylesheet's text (injected so callers/tests control the
    network); stylesheet fetch failures are skipped, not fatal."""
    ranked, _context = _extract_weighted(html, base_url=base_url, fetch=fetch)
    return ranked


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
    """Slot context-ranked candidates (from extract_candidate_colors) into the
    six roles by luminance/saturation/warmth. `candidates` arrives strongest
    brand signal first, so index 0 is the color the page most deliberately
    paints -- not merely its most frequent token. Always returns every role;
    leans on _FALLBACK for any role the page gives nothing usable for."""
    roles = dict(_FALLBACK)
    if not candidates:
        return roles

    colors = [c for c, _w in candidates]
    saturated = [c for c in colors if _saturation(c) >= 0.2]

    # Primary + secondary: the top two *saturated* brand-ranked colors (fall
    # back to the overall ranking when the page is largely gray).
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


# Cap on redirect hops we'll follow -- each is re-validated by _guard_public_url.
MAX_REDIRECTS = 5


def _guard_public_url(url):
    """Raise ColorDeriveError unless `url` is an http(s) URL whose host resolves
    ENTIRELY to public IP addresses. Blocks the SSRF/port-scan surface a
    manager-supplied URL would otherwise open: loopback (localhost), private
    ranges, link-local (incl. the 169.254.169.254 cloud-metadata endpoint),
    and other reserved/multicast space. Applied to the page URL, every redirect
    hop, and every linked stylesheet."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ColorDeriveError("Only http(s) web addresses can be read.")
    host = parsed.hostname
    if not host:
        raise ColorDeriveError("That address has no host to read.")
    try:
        infos = socket.getaddrinfo(host, parsed.port or 80, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise ColorDeriveError(f"Couldn't resolve {host}.") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global or ip.is_multicast:
            raise ColorDeriveError(
                "That address points at a private or internal host, which can't be read."
            )


def _http_fetch(url):
    """Fetch a URL's text via `requests`, capped at MAX_FETCH_BYTES. Every URL
    (the page, each redirect hop, and each linked stylesheet -- all arrive
    here) is SSRF-guarded first (_guard_public_url). Redirects are followed
    manually so each Location is re-validated before it's requested. Outbound
    goes through the environment's configured proxy (requests honors
    HTTPS_PROXY)."""
    import requests

    headers = {"User-Agent": "boxo.show color-scheme agent"}
    for _hop in range(MAX_REDIRECTS + 1):
        _guard_public_url(url)
        resp = requests.get(
            url, timeout=FETCH_TIMEOUT, headers=headers, stream=True, allow_redirects=False
        )
        if resp.is_redirect and resp.headers.get("Location"):
            url = urljoin(url, resp.headers["Location"])
            resp.close()
            continue
        resp.raise_for_status()
        content = resp.raw.read(MAX_FETCH_BYTES, decode_content=True) or b""
        return content.decode(resp.encoding or "utf-8", errors="replace")
    raise ColorDeriveError("Too many redirects.")


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

    candidates, context = _extract_weighted(html, base_url=url, fetch=fetch)
    roles = assign_roles(candidates)
    scheme = {
        "name": _default_name(url),
        "roles": roles,
        "source_url": url,
        "candidates": candidates[:24],
        "context": context,
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
    # Give Claude the *context* each color was found in, not just a count -- so
    # it can tell a header/button brand color from a default link color rather
    # than being anchored on raw frequency.
    context = scheme.get("context", {})
    swatches = ", ".join(
        f"{c} (used {w}x, mostly {context.get(c, 'markup')})" for c, w in scheme["candidates"]
    )
    prompt = (
        "You are a brand designer. From these colors extracted from a theater's "
        f"homepage ({scheme['source_url']}), choose a cohesive six-role palette.\n\n"
        "Colors, strongest brand signal first (with where each is used on the "
        f"page):\n{swatches}\n\n"
        f"A first-pass heuristic proposed: {json.dumps(scheme['roles'])}\n\n"
        "Refine into the six roles: primary (main brand), secondary (supporting), "
        "dark_accent (deep shade), feature_accent (warm highlight/CTA), light_neutral "
        "(light background), neutral (near-black text). Judge each color by CONTEXT, "
        "not how often it appears: a color used on brand surfaces, the header/nav, "
        "buttons, or a named brand variable is a strong `primary` candidate; a color "
        "used only for links/anchor text is almost always a framework default -- do "
        "NOT make it the primary. Prefer colors actually present on the page; only "
        "invent a color if a role has no good match. Give a short evocative name."
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
