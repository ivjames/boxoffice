"""Stage 1 + 2 of the color-derive agent: pull every color token out of a page
(context-scored, not raw-counted) and slot the ranked candidates into the six
palette roles.

See the package's `__init__.py` / the original module docstring for the full
two-stage design rationale.
"""

import logging
import re
from collections import defaultdict
from urllib.parse import urljoin, urlparse

logger = logging.getLogger(__name__)

# Don't chase a whole site -- the homepage plus a handful of its stylesheets is
# plenty to read a brand's colors off, and bounds both time and bytes.
MAX_STYLESHEETS = 5

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

# A rule that renders nothing visible -- `opacity:0` (not 0.x), `visibility:
# hidden`, `display:none`. Its colors aren't part of the visible brand, so we
# skip the whole rule. This is what strips the `opacity:0` focus/skip-link
# elements framework sites hide a default accent color inside.
_HIDDEN_RE = re.compile(
    r"opacity\s*:\s*0(?![.\d])|visibility\s*:\s*hidden|display\s*:\s*none",
    re.IGNORECASE,
)
# Screen-reader-only utility selectors -- visually hidden a11y text.
_HIDDEN_SEL_TOKENS = {"sr", "visually", "screenreader", "offscreen"}

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
# Heading elements/classes -- brand color lives in titles more than body copy.
_HEADING_SEL_TOKENS = {"h1", "h2", "h3", "title", "heading", "headline"}
# CSS custom properties a theme names for its brand, its links, or -- crucially
# on framework sites (Wix/Squarespace) -- its interaction *chrome* (focus rings,
# shadows). Those chrome vars are where a hidden platform-default blue hides.
_BRAND_VAR_TOKENS = {"brand", "primary", "accent", "theme", "logo", "main"}
_LINK_VAR_TOKENS = {"link", "links", "anchor"}
_STATE_VAR_TOKENS = {
    "focus", "ring", "shadow", "outline", "hover", "active", "caret",
    "selection", "scrollbar", "disabled",
}

_BG_PROPS = {"background", "background-color", "background-image"}
# True edges (borders) can be brand-hued, so they count a little.
_BORDER_PROPS = {
    "border", "border-color", "border-top-color", "border-right-color",
    "border-bottom-color", "border-left-color", "border-block-color",
    "border-inline-color", "column-rule-color",
}
# Chrome: focus rings, shadows, carets, underline/selection tints. These carry a
# platform's *default* accent (the Wix focus-ring blue), never the brand, so
# they score zero -- extracting them is what let a hidden blue win `primary`.
_CHROME_PROPS = {
    "box-shadow", "text-shadow", "outline", "outline-color",
    "-webkit-tap-highlight-color", "caret-color", "-webkit-text-stroke-color",
    "text-decoration-color", "column-rule",
}


def _context_score(selector, prop):
    """How strong a brand signal a color is, given the CSS (selector, property)
    it was declared in. Returns `(weight, label)`; weight 0 means "seen, but
    never let this steer a role" -- link text, interaction chrome, and opaque
    framework state. `label` is a human context hint passed on to Claude's
    refinement."""
    sel = (selector or "").strip().lower()
    prop = (prop or "").strip().lower()

    # Custom properties: the theme has *named* the color's job -- trust the name.
    if prop.startswith("--"):
        tokens = set(_TOKEN_SPLIT_RE.split(prop[2:]))
        if tokens & (_LINK_VAR_TOKENS | _STATE_VAR_TOKENS):
            return 0, "chrome variable"
        if tokens & _BRAND_VAR_TOKENS:
            return 12, "brand variable"
        # An opaquely-named var (`--color_18`, `--wst-...`) is just a palette
        # slot -- weak signal, must not outrank a color the page actually paints.
        return 1, "variable"

    # Focus rings / shadows / carets: platform-default accents, never brand.
    if prop in _CHROME_PROPS:
        return 0, "chrome"

    tokens = set(_TOKEN_SPLIT_RE.split(sel))
    interactive = bool(_PSEUDO_LINK_RE.search(sel)) or bool(tokens & _LINK_SEL_TOKENS)

    if prop == "color":
        # Link/anchor text: the framework-default culprit. Never rank it.
        if interactive:
            return 0, "link"
        if tokens & _HEADING_SEL_TOKENS:
            return 2, "heading text"
        return 1, "text"

    if prop in _BG_PROPS:
        if tokens & _BRAND_SEL_TOKENS:
            return 10, "brand surface"
        if tokens & _SURFACE_SEL_TOKENS:
            return 6, "surface"
        if interactive:
            return 2, "interactive surface"
        return 4, "background"

    if prop in _BORDER_PROPS:
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
        if _HIDDEN_RE.search(css_text):  # inline style hides the element
            return
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
        if _HIDDEN_RE.search(body):  # invisible rule -- not part of the brand
            continue
        if set(_TOKEN_SPLIT_RE.split(selector.lower())) & _HIDDEN_SEL_TOKENS:
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


def _default_name(url):
    host = (urlparse(url).hostname or "").replace("www.", "")
    label = host.split(".")[0].replace("-", " ").title() if host else "Homepage"
    return f"{label} palette"
