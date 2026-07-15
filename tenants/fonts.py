"""The tenant typography catalog: a curated set of heading/body fonts a
theater can pick to match its brand, alongside the six-role color palette.

Each font is a CSS `font-family` stack that ALWAYS ends in a robust generic
fallback, so a storefront stays readable even if a web font fails to load.
Fonts sourced from Google Fonts carry a `google` value (the family + weights
for the stylesheet request); base.html emits a single combined Google Fonts
<link> for whichever families the org actually selected, and nothing loads for
the system-stack options. Two roles are enough to shape a brand's look without
overwhelming the picker: `heading` and `body`.

Applied via CSS variables (--heading-font / --body-font) the same way colors
are (see templates/base.html + static/css/app.css). Kept deliberately
tenant-agnostic and self-contained: no font is required, the defaults are the
system stacks the storefront already used.
"""

# key -> {label, category, stack, google}. `google` is the Google Fonts
# `family=` spec (or "" for system stacks that need no network load). Order
# here is the order the dashboard dropdowns present them.
FONTS = {
    "system-sans": {
        "label": "System Sans (default)",
        "category": "Sans-serif",
        "stack": "system-ui, -apple-system, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif",
        "google": "",
    },
    "system-serif": {
        "label": "System Serif",
        "category": "Serif",
        "stack": "Georgia, Cambria, 'Times New Roman', Times, serif",
        "google": "",
    },
    "playfair": {
        "label": "Playfair Display",
        "category": "Display serif",
        "stack": "'Playfair Display', Georgia, serif",
        "google": "Playfair+Display:wght@400;600;700",
    },
    "cormorant": {
        "label": "Cormorant Garamond",
        "category": "Elegant serif",
        "stack": "'Cormorant Garamond', 'Times New Roman', serif",
        "google": "Cormorant+Garamond:wght@400;500;600;700",
    },
    "libre-baskerville": {
        "label": "Libre Baskerville",
        "category": "Classic serif",
        "stack": "'Libre Baskerville', Georgia, serif",
        "google": "Libre+Baskerville:wght@400;700",
    },
    "lora": {
        "label": "Lora",
        "category": "Serif",
        "stack": "'Lora', Georgia, serif",
        "google": "Lora:wght@400;500;600;700",
    },
    "eb-garamond": {
        "label": "EB Garamond",
        "category": "Old-style serif",
        "stack": "'EB Garamond', Garamond, serif",
        "google": "EB+Garamond:wght@400;500;600;700",
    },
    "montserrat": {
        "label": "Montserrat",
        "category": "Geometric sans",
        "stack": "'Montserrat', 'Segoe UI', sans-serif",
        "google": "Montserrat:wght@400;500;600;700",
    },
    "poppins": {
        "label": "Poppins",
        "category": "Geometric sans",
        "stack": "'Poppins', 'Segoe UI', sans-serif",
        "google": "Poppins:wght@400;500;600;700",
    },
    "inter": {
        "label": "Inter",
        "category": "Neutral sans",
        "stack": "'Inter', system-ui, sans-serif",
        "google": "Inter:wght@400;500;600;700",
    },
    "work-sans": {
        "label": "Work Sans",
        "category": "Neutral sans",
        "stack": "'Work Sans', system-ui, sans-serif",
        "google": "Work+Sans:wght@400;500;600;700",
    },
    "raleway": {
        "label": "Raleway",
        "category": "Elegant sans",
        "stack": "'Raleway', 'Segoe UI', sans-serif",
        "google": "Raleway:wght@400;500;600;700",
    },
    "oswald": {
        "label": "Oswald",
        "category": "Condensed display",
        "stack": "'Oswald', 'Arial Narrow', sans-serif",
        "google": "Oswald:wght@400;500;600;700",
    },
    "bebas-neue": {
        "label": "Bebas Neue",
        "category": "Condensed display",
        "stack": "'Bebas Neue', 'Oswald', 'Arial Narrow', sans-serif",
        "google": "Bebas+Neue",
    },
    "abril-fatface": {
        "label": "Abril Fatface",
        "category": "Poster display",
        "stack": "'Abril Fatface', Georgia, serif",
        "google": "Abril+Fatface",
    },
}

DEFAULT_HEADING_FONT = "system-sans"
DEFAULT_BODY_FONT = "system-sans"

# (key, label) pairs for a form ChoiceField -- validates the stored value
# against the catalog, not just renders a dropdown.
FONT_CHOICES = [(key, spec["label"]) for key, spec in FONTS.items()]


def font_stack(key):
    """The CSS font-family stack for a font key, falling back to the system
    sans stack for an unknown/legacy key so a bad value never yields an empty
    `font-family`."""
    return FONTS.get(key, FONTS[DEFAULT_HEADING_FONT])["stack"]


def google_families(*keys):
    """The de-duplicated Google Fonts `family=` specs for the given font keys
    (skipping system stacks). Used by base.html to build one combined
    stylesheet request for exactly the fonts a tenant selected."""
    seen = []
    for key in keys:
        spec = FONTS.get(key)
        if spec and spec["google"] and spec["google"] not in seen:
            seen.append(spec["google"])
    return seen
