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
venues.chart_parsing's opt-in) -- hand the result to Claude to name and refine.
When a headless browser is available it renders the homepage to a screenshot
and lets Claude choose the palette from what's actually VISIBLE (the durable
answer for monochrome brands and framework sites, where static CSS can't tell a
faint real brand color from stronger framework noise); with no browser it falls
back to a text-only refinement of the extracted candidates. With no key at all
it returns the complete deterministic scheme; the network fetch is the only hard
dependency -- the browser and the API are both optional, best-effort upgrades.

Nothing here writes to the database. The dashboard view decides whether to
apply the derived colors or save them as a ColorScheme.

This package is a cohesion split of what used to be a single
`color_extraction.py` module, kept behind this stable facade:

- `scoring`: color math + context-aware CSS scraping/scoring + role assignment
  (`ColorDeriveError`, `extract_candidate_colors`, `assign_roles`, ...).
- `_netfetch`: the SSRF guard and the plain HTTP fetch.
- `_screenshot`: headless-Chromium homepage rendering + PNG downscaling.
- `_vision`: the Anthropic-SDK vision/text refinement step.
- `derive`: `derive_scheme_from_url`, the orchestrator tying the above together.

Everything importable from the old flat module remains importable from here.
"""

from .derive import derive_scheme_from_url
from .scoring import (
    ColorDeriveError,
    _colors_in_value,
    _context_score,
    _default_name,
    _extract_weighted,
    _hex_rgb_in,
    _luminance,
    _normalize_hex,
    _rgb,
    _saturation,
    _tally_css,
    _warmth,
    assign_roles,
    extract_candidate_colors,
)
from ._netfetch import _guard_public_url, _http_fetch, _is_public_url
from ._screenshot import _downscale_png, _find_chromium_executable, render_homepage_png
from ._vision import _apply_refinement, _derive_with_vision, _refine_with_claude

__all__ = [
    "ColorDeriveError",
    "derive_scheme_from_url",
    "extract_candidate_colors",
    "assign_roles",
    "_guard_public_url",
    "_is_public_url",
    "_http_fetch",
    "render_homepage_png",
    "_find_chromium_executable",
    "_downscale_png",
    "_apply_refinement",
    "_derive_with_vision",
    "_refine_with_claude",
    "_normalize_hex",
    "_rgb",
    "_luminance",
    "_saturation",
    "_warmth",
    "_context_score",
    "_colors_in_value",
    "_hex_rgb_in",
    "_tally_css",
    "_extract_weighted",
    "_default_name",
]
