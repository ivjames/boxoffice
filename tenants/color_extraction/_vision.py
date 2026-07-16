"""Claude refinement of the deterministic scheme -- vision-first (from a
homepage screenshot), text fallback (from the extracted candidates alone).
Both are opt-in: only reached when settings.ANTHROPIC_API_KEY is configured,
and both are best-effort -- the caller (derive.derive_scheme_from_url)
swallows any failure and keeps the deterministic result."""

from django.conf import settings

from ..color_schemes import ROLE_KEYS
from .scoring import _normalize_hex

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


def _apply_refinement(scheme, data, method):
    """Copy a validated model response (name + role hexes) onto the scheme."""
    scheme["name"] = data.get("name") or scheme["name"]
    for role in ROLE_KEYS:
        candidate = _normalize_hex(str(data.get(role, "")))
        if candidate:
            scheme["roles"][role] = candidate
    scheme["method"] = method


def _derive_with_vision(scheme, png_bytes):
    """In-place: show Claude a screenshot of the homepage and let it choose the
    six-role palette from what's VISIBLE, using the extracted candidates only as
    a hint. This is what a static-CSS heuristic can't do -- see a black-and-white
    brand as black-and-white, and ignore framework colors that never render.
    Best-effort; the caller swallows failures."""
    import base64
    import json

    import anthropic

    client = anthropic.Anthropic(api_key=getattr(settings, "ANTHROPIC_API_KEY", "") or None)
    model = getattr(settings, "CHART_PARSING_MODEL", "claude-opus-4-8")
    context = scheme.get("context", {})
    swatches = ", ".join(f"{c} ({context.get(c, 'markup')})" for c, _w in scheme["candidates"])
    prompt = (
        "You are a brand designer choosing a six-role color palette for a theater's "
        "ticketing storefront, so it matches the theater's own website. Attached is a "
        f"screenshot of the homepage of {scheme['source_url']}.\n\n"
        "Colors pulled from the page's CSS (a hint only -- this list includes framework "
        "defaults, focus/hover chrome, and decorative colors that may NOT be part of the "
        f"visible brand):\n{swatches}\n\n"
        "Look at the SCREENSHOT and identify the colors the brand actually uses -- its "
        "logo, header/nav, primary buttons, and the dominant accents a visitor sees. Then "
        "assign the six roles: primary (main brand), secondary (supporting), feature_accent "
        "(warm CTA/highlight), dark_accent (deep shade), light_neutral (light background), "
        "neutral (near-black text).\n"
        "- Judge by what is VISIBLE in the screenshot, not by how often a color appears in CSS.\n"
        "- If the site is essentially black-and-white or monochrome, return neutral roles "
        "that reflect that (a black or near-black primary) rather than inventing an accent.\n"
        "- Ignore browser chrome, embedded maps/widgets, and incidental colors inside photos.\n"
        "- Prefer exact hex values from the hint list when they match what you see; otherwise "
        "use the hex you observe.\n"
        "Return #rrggbb for every role and a short evocative name."
    )
    image_block = {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/png",
            "data": base64.standard_b64encode(png_bytes).decode("ascii"),
        },
    }
    response = client.messages.create(
        model=model,
        max_tokens=1024,
        output_config={"format": {"type": "json_schema", "schema": _REFINE_SCHEMA}},
        messages=[{"role": "user", "content": [image_block, {"type": "text", "text": prompt}]}],
    )
    if response.stop_reason == "refusal":
        return
    text = "".join(block.text for block in response.content if getattr(block, "type", None) == "text")
    _apply_refinement(scheme, json.loads(text), "vision")


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
    _apply_refinement(scheme, json.loads(text), "text")
