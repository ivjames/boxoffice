"""The six-role color model shared by tenant branding, the built-in preset
catalog, and the derive-from-homepage agent (tenants/color_extraction.py).

A "scheme" is six named roles. Two of them map onto the two legacy
Organization color fields that `templates/base.html` and `static/css/app.css`
have always keyed on, so applying a scheme stays backward compatible and the
storefront keeps rendering with no CSS rename:

    role            label (UI)      Organization field      CSS variable
    --------------  -------------   ---------------------   ----------------------
    primary         Primary         primary_color           --primary-color  (legacy)
    secondary       Secondary       secondary_color         --secondary-color
    feature_accent  Feature Accent  accent_color            --accent-color   (legacy)
    dark_accent     Dark Accent     dark_accent_color       --dark-accent-color
    light_neutral   Light Neutral   light_neutral_color     --light-neutral-color
    neutral         Neutral         neutral_color           --neutral-color

The `feature_accent` role is the eye-catching call-to-action / highlight pop
(buttons, links) -- exactly the role app.css's --accent-color has always
played -- so it maps onto the existing `accent_color` field rather than adding a
redundant one. Everything downstream (the ColorScheme model, the branding form,
the derive agent) speaks in these six role keys; ROLE_TO_ORG_FIELD is the
single place the mapping to storage lives.
"""

from .color_generator import build_wcag_schemes

# (role key, human label, Organization field it applies onto). Order matches
# the client's palette spec (Feature Accent precedes Dark Accent) and is the
# display order everywhere: admin, the branding form, the preset swatches.
COLOR_ROLES = [
    ("primary", "Primary", "primary_color"),
    ("secondary", "Secondary", "secondary_color"),
    ("feature_accent", "Feature Accent", "accent_color"),
    ("dark_accent", "Dark Accent", "dark_accent_color"),
    ("light_neutral", "Light Neutral", "light_neutral_color"),
    ("neutral", "Neutral", "neutral_color"),
]

ROLE_KEYS = [key for key, _label, _field in COLOR_ROLES]
ROLE_LABELS = {key: label for key, label, _field in COLOR_ROLES}
ROLE_TO_ORG_FIELD = {key: field for key, _label, field in COLOR_ROLES}

# Matches "#rgb" and "#rrggbb" (case-insensitive). The single source of truth
# for "is this a valid stored color" -- reused by the model field validator
# and the dashboard forms.
HEX_COLOR_RE = r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$"


# The design source: The Roxy Theater's curated 36-scheme palette matrix
# (spectrum order). These are the exact brand colors; the shipped BUILTIN_SCHEMES
# below is derived from them by the WCAG generator (tenants.color_generator),
# which nudges ONLY the two neutral/text roles for contrast (best-of-two per
# surface) and leaves the four brand roles untouched. Each entry is (slug, name,
# roles) in the six-key role shape above; `feature_accent` holds the Feature
# Accent.
SOURCE_SCHEMES = [
    ("ruby-velvet", "Ruby Velvet", {
        "primary": "#6A1E32", "secondary": "#A64868", "feature_accent": "#4E7773",
        "dark_accent": "#2C0E17", "light_neutral": "#F7EFE3", "neutral": "#181312"}),
    ("crimson-cabaret", "Crimson Cabaret", {
        "primary": "#9A2D3F", "secondary": "#C77A86", "feature_accent": "#4D7C87",
        "dark_accent": "#461723", "light_neutral": "#FAF0EC", "neutral": "#211719"}),
    ("cherry-spotlight", "Cherry Spotlight", {
        "primary": "#B33A4A", "secondary": "#D98B96", "feature_accent": "#57928F",
        "dark_accent": "#541923", "light_neutral": "#FFF4F3", "neutral": "#2A1D21"}),
    ("blush-clay", "Blush & Clay", {
        "primary": "#D89AA6", "secondary": "#EBC2C8", "feature_accent": "#557873",
        "dark_accent": "#532B38", "light_neutral": "#FFF8F4", "neutral": "#30292B"}),
    ("brick-playhouse", "Brick Playhouse", {
        "primary": "#A84F42", "secondary": "#CE8173", "feature_accent": "#5F8E91",
        "dark_accent": "#542B26", "light_neutral": "#FAEEE7", "neutral": "#28201E"}),
    ("sunset-marquee", "Sunset Marquee", {
        "primary": "#C75A3A", "secondary": "#F0A068", "feature_accent": "#5877A3",
        "dark_accent": "#57281E", "light_neutral": "#F8E9D3", "neutral": "#261B16"}),
    ("rust-proscenium", "Rust Proscenium", {
        "primary": "#B25D38", "secondary": "#D28A62", "feature_accent": "#557697",
        "dark_accent": "#56301F", "light_neutral": "#F8EBDD", "neutral": "#2A1C16"}),
    ("copper-house", "Copper House", {
        "primary": "#A65B32", "secondary": "#D3A67D", "feature_accent": "#5E8A82",
        "dark_accent": "#3B241A", "light_neutral": "#F5E8D7", "neutral": "#201714"}),
    ("pumpkin-revue", "Pumpkin Revue", {
        "primary": "#D47732", "secondary": "#E9A96D", "feature_accent": "#476B93",
        "dark_accent": "#5B351C", "light_neutral": "#FFF0D9", "neutral": "#2B211A"}),
    ("apricot-salon", "Apricot Salon", {
        "primary": "#E8A06F", "secondary": "#F4C2A2", "feature_accent": "#6874A0",
        "dark_accent": "#6D3928", "light_neutral": "#FFF4E7", "neutral": "#33241F"}),
    ("golden-matinee", "Golden Matinee", {
        "primary": "#C8942D", "secondary": "#E2BD67", "feature_accent": "#4C6486",
        "dark_accent": "#5A421E", "light_neutral": "#FFF4D6", "neutral": "#241E16"}),
    ("mustard-moderne", "Mustard Moderne", {
        "primary": "#B99A32", "secondary": "#D8C77B", "feature_accent": "#62678D",
        "dark_accent": "#50451F", "light_neutral": "#FBF4D8", "neutral": "#26231A"}),
    ("ivory-garnet", "Ivory & Garnet", {
        "primary": "#EFE6D4", "secondary": "#BCA98D", "feature_accent": "#3D587D",
        "dark_accent": "#4A3528", "light_neutral": "#FCFBF7", "neutral": "#2D2724"}),
    ("chartreuse-cabaret", "Chartreuse Cabaret", {
        "primary": "#8EA341", "secondary": "#C3D28A", "feature_accent": "#7A668D",
        "dark_accent": "#3B4720", "light_neutral": "#F5F8E7", "neutral": "#242A1E"}),
    ("olive-revue", "Olive Revue", {
        "primary": "#7A8048", "secondary": "#B9C29B", "feature_accent": "#76505E",
        "dark_accent": "#3A4025", "light_neutral": "#F6F1DF", "neutral": "#1A1817"}),
    ("forest-manor", "Forest Manor", {
        "primary": "#264D3B", "secondary": "#7B9772", "feature_accent": "#B87965",
        "dark_accent": "#182A23", "light_neutral": "#F2ECDF", "neutral": "#20211F"}),
    ("emerald-palace", "Emerald Palace", {
        "primary": "#146B52", "secondary": "#6EA67C", "feature_accent": "#8F729E",
        "dark_accent": "#123128", "light_neutral": "#F5F0E6", "neutral": "#1C1D21"}),
    ("sage-conservatory", "Sage Conservatory", {
        "primary": "#9FB59C", "secondary": "#C6D3C0", "feature_accent": "#8C7A89",
        "dark_accent": "#294538", "light_neutral": "#F7FAF3", "neutral": "#29312E"}),
    ("mint-salon", "Mint Salon", {
        "primary": "#9AC8B3", "secondary": "#CFE4D8", "feature_accent": "#B77E8A",
        "dark_accent": "#315247", "light_neutral": "#F7FCF9", "neutral": "#26312D"}),
    ("peacock-luxe", "Peacock Luxe", {
        "primary": "#0D6B73", "secondary": "#5DB5B3", "feature_accent": "#8B536D",
        "dark_accent": "#0A3438", "light_neutral": "#F5F6F3", "neutral": "#1A2428"}),
    ("sea-glass-foyer", "Sea Glass Foyer", {
        "primary": "#7DBAB4", "secondary": "#B9D9D5", "feature_accent": "#C38A7A",
        "dark_accent": "#24504E", "light_neutral": "#F7FCFA", "neutral": "#263433"}),
    ("aqua-pavilion", "Aqua Pavilion", {
        "primary": "#58AEB3", "secondary": "#A8D6D7", "feature_accent": "#C77C70",
        "dark_accent": "#24565A", "light_neutral": "#F4FCFC", "neutral": "#243235"}),
    ("cyan-electric", "Cyan Electric", {
        "primary": "#249DB5", "secondary": "#77C5D5", "feature_accent": "#B96C46",
        "dark_accent": "#174A59", "light_neutral": "#F3FBFD", "neutral": "#18282E"}),
    ("sapphire-night", "Sapphire Night", {
        "primary": "#244C9A", "secondary": "#6D92D9", "feature_accent": "#BE934F",
        "dark_accent": "#14213B", "light_neutral": "#F2F5FA", "neutral": "#11131A"}),
    ("powder-blue", "Powder Blue", {
        "primary": "#A9C3DD", "secondary": "#D2E1EF", "feature_accent": "#C9887E",
        "dark_accent": "#24384F", "light_neutral": "#FAFCFE", "neutral": "#26303A"}),
    ("modern-luxe", "Modern Luxe", {
        "primary": "#465A78", "secondary": "#9087B5", "feature_accent": "#B58D55",
        "dark_accent": "#29384D", "light_neutral": "#F5F6F8", "neutral": "#25272B"}),
    ("indigo-house", "Indigo House", {
        "primary": "#3F497D", "secondary": "#818DB8", "feature_accent": "#BE9651",
        "dark_accent": "#252A4B", "light_neutral": "#F1F3FA", "neutral": "#1B1D29"}),
    ("periwinkle-stage", "Periwinkle Stage", {
        "primary": "#8D9CC7", "secondary": "#BBC3DE", "feature_accent": "#B87A84",
        "dark_accent": "#363C59", "light_neutral": "#F8F9FD", "neutral": "#272A38"}),
    ("art-deco-royal", "Art Deco Royal", {
        "primary": "#4B2E83", "secondary": "#7E5BA7", "feature_accent": "#517D78",
        "dark_accent": "#2A132F", "light_neutral": "#F2E8D6", "neutral": "#0E0E12"}),
    ("lilac-premiere", "Lilac Premiere", {
        "primary": "#B39AC9", "secondary": "#D4C3E2", "feature_accent": "#5E8576",
        "dark_accent": "#46304F", "light_neutral": "#FCF8FE", "neutral": "#302934"}),
    ("plum-velvet", "Plum Velvet", {
        "primary": "#764567", "secondary": "#A97D9D", "feature_accent": "#5E8178",
        "dark_accent": "#3E2237", "light_neutral": "#FAF1F7", "neutral": "#2A2027"}),
    ("eggplant-opera", "Eggplant Opera", {
        "primary": "#56334F", "secondary": "#8D6C86", "feature_accent": "#66817F",
        "dark_accent": "#2B1828", "light_neutral": "#F7EFF4", "neutral": "#211A20"}),
    ("vintage-cinema", "Vintage Cinema", {
        "primary": "#5A1C24", "secondary": "#7A8048", "feature_accent": "#5A7083",
        "dark_accent": "#2B2019", "light_neutral": "#EFE4C8", "neutral": "#1A1817"}),
    ("rose-quartz", "Rose Quartz", {
        "primary": "#CFA4AE", "secondary": "#E9CDD3", "feature_accent": "#557B72",
        "dark_accent": "#56323B", "light_neutral": "#FFF8F9", "neutral": "#33282C"}),
    ("midnight-noir", "Midnight Noir", {
        "primary": "#222329", "secondary": "#626873", "feature_accent": "#8B3944",
        "dark_accent": "#0D0D10", "light_neutral": "#F4F3EF", "neutral": "#18191C"}),
    ("silver-screen", "Silver Screen", {
        "primary": "#B6B8BD", "secondary": "#D8D9DC", "feature_accent": "#6A7F94",
        "dark_accent": "#41444A", "light_neutral": "#FAFAFA", "neutral": "#202124"}),
]

# The shipped catalog: SOURCE_SCHEMES with the two neutral/text roles nudged for
# WCAG contrast (best-of-two per surface -- see tenants.color_generator).
# Everything else in the app imports BUILTIN_SCHEMES and is unaffected by the
# source/generated split. Computed once at import -- pure and deterministic.
BUILTIN_SCHEMES = build_wcag_schemes(SOURCE_SCHEMES)


def sync_presets(ColorScheme):
    """Make the built-in preset rows exactly match BUILTIN_SCHEMES: upsert every
    scheme (keyed on organization=NULL + slug) and delete any preset whose slug
    is no longer in the catalog. Idempotent. Shared by the seed migration and
    the `seed_color_schemes` command; takes the model class so a migration can
    pass its historical `apps.get_model` version. Returns (created, updated,
    deleted). Custom (org-owned) schemes are never touched -- the prune is
    scoped to organization=NULL, is_preset=True."""
    slugs = [slug for slug, _name, _roles in BUILTIN_SCHEMES]
    created = updated = 0
    for index, (slug, name, roles) in enumerate(BUILTIN_SCHEMES):
        _obj, was_created = ColorScheme.objects.update_or_create(
            organization=None,
            slug=slug,
            defaults={"name": name, "is_preset": True, "ordering": index, **roles},
        )
        created += was_created
        updated += not was_created
    deleted, _ = (
        ColorScheme.objects.filter(organization=None, is_preset=True)
        .exclude(slug__in=slugs)
        .delete()
    )
    return created, updated, deleted
