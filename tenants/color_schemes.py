"""The six-role color model shared by tenant branding, the built-in preset
catalog, and the derive-from-homepage agent (tenants/color_extraction.py).

A "scheme" is six named roles. Two of them map onto the two legacy
Organization color fields that `templates/base.html` and `static/css/app.css`
have always keyed on, so applying a scheme stays backward compatible and the
storefront keeps rendering with no CSS rename:

    role           label (UI)      Organization field      CSS variable
    -------------   -------------   ---------------------   ----------------------
    primary         Primary         primary_color           --primary-color  (legacy)
    secondary       Secondary       secondary_color         --secondary-color
    metallic        Feature Accent  accent_color            --accent-color   (legacy)
    dark_accent     Dark Accent     dark_accent_color       --dark-accent-color
    light_neutral   Light Neutral   light_neutral_color     --light-neutral-color
    neutral         Neutral         neutral_color           --neutral-color

The `metallic` role KEY is historical; it's the eye-catching call-to-action /
highlight pop (buttons, links) the client calls the "Feature Accent" -- exactly
the role app.css's --accent-color has always played -- so it maps onto the
existing `accent_color` field rather than adding a redundant one, and is
LABELLED "Feature Accent" everywhere in the UI. Everything downstream (the
ColorScheme model, the branding form, the derive agent) speaks in these six
role keys; ROLE_TO_ORG_FIELD is the single place the mapping to storage lives.
"""

# (role key, human label, Organization field it applies onto). Order matches
# the client's palette spec (Feature Accent precedes Dark Accent) and is the
# display order everywhere: admin, the branding form, the preset swatches.
COLOR_ROLES = [
    ("primary", "Primary", "primary_color"),
    ("secondary", "Secondary", "secondary_color"),
    ("metallic", "Feature Accent", "accent_color"),
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


# The built-in preset catalog: The Roxy Theater's curated 24-scheme palette
# spec (spectrum order; every swatch label WCAG AA against its fill, min ratio
# 4.61:1). Seeded as ColorScheme rows with organization=NULL, is_preset=True by
# the tenants migration and kept in sync (upsert + prune) by sync_presets /
# the `seed_color_schemes` command. Each entry is (slug, name, roles) in the
# six-key role shape above; `metallic` holds the spec's "Feature Accent" color.
BUILTIN_SCHEMES = [
    ("ruby-velvet", "Ruby Velvet", {
        "primary": "#6A1E32", "secondary": "#A64868", "metallic": "#E4A3A8",
        "dark_accent": "#2C0E17", "light_neutral": "#F7EFE3", "neutral": "#181312"}),
    ("crimson-cabaret", "Crimson Cabaret", {
        "primary": "#9A2D3F", "secondary": "#C77A86", "metallic": "#F08A72",
        "dark_accent": "#461723", "light_neutral": "#FAF0EC", "neutral": "#211719"}),
    ("blush-teal", "Blush & Teal", {
        "primary": "#D89AA6", "secondary": "#EBC2C8", "metallic": "#1F6664",
        "dark_accent": "#532B38", "light_neutral": "#FFF8F4", "neutral": "#30292B"}),
    ("sunset-marquee", "Sunset Marquee", {
        "primary": "#C75A3A", "secondary": "#F0A068", "metallic": "#F4C95D",
        "dark_accent": "#57281E", "light_neutral": "#F8E9D3", "neutral": "#261B16"}),
    ("copper-house", "Copper House", {
        "primary": "#A65B32", "secondary": "#D3A67D", "metallic": "#3C817A",
        "dark_accent": "#3B241A", "light_neutral": "#F5E8D7", "neutral": "#201714"}),
    ("apricot-salon", "Apricot Salon", {
        "primary": "#E8A06F", "secondary": "#F4C2A2", "metallic": "#71405C",
        "dark_accent": "#6D3928", "light_neutral": "#FFF4E7", "neutral": "#33241F"}),
    ("golden-matinee", "Golden Matinee", {
        "primary": "#C8942D", "secondary": "#E2BD67", "metallic": "#8A2F38",
        "dark_accent": "#5A421E", "light_neutral": "#FFF4D6", "neutral": "#241E16"}),
    ("ivory-sapphire", "Ivory & Sapphire", {
        "primary": "#EFE6D4", "secondary": "#BCA98D", "metallic": "#2F4F7F",
        "dark_accent": "#4A3528", "light_neutral": "#FCFBF7", "neutral": "#2D2724"}),
    ("olive-revue", "Olive Revue", {
        "primary": "#7A8048", "secondary": "#B9C29B", "metallic": "#9B4A3C",
        "dark_accent": "#3A4025", "light_neutral": "#F6F1DF", "neutral": "#1A1817"}),
    ("forest-manor", "Forest Manor", {
        "primary": "#264D3B", "secondary": "#7B9772", "metallic": "#D09838",
        "dark_accent": "#182A23", "light_neutral": "#F2ECDF", "neutral": "#20211F"}),
    ("emerald-palace", "Emerald Palace", {
        "primary": "#146B52", "secondary": "#6EA67C", "metallic": "#B977A5",
        "dark_accent": "#123128", "light_neutral": "#F5F0E6", "neutral": "#1C1D21"}),
    ("sage-conservatory", "Sage Conservatory", {
        "primary": "#9FB59C", "secondary": "#C6D3C0", "metallic": "#C97868",
        "dark_accent": "#294538", "light_neutral": "#F7FAF3", "neutral": "#29312E"}),
    ("peacock-luxe", "Peacock Luxe", {
        "primary": "#0D6B73", "secondary": "#5DB5B3", "metallic": "#B64678",
        "dark_accent": "#0A3438", "light_neutral": "#F5F6F3", "neutral": "#1A2428"}),
    ("sea-glass-foyer", "Sea Glass Foyer", {
        "primary": "#7DBAB4", "secondary": "#B9D9D5", "metallic": "#E59A78",
        "dark_accent": "#24504E", "light_neutral": "#F7FCFA", "neutral": "#263433"}),
    ("sapphire-night", "Sapphire Night", {
        "primary": "#244C9A", "secondary": "#6D92D9", "metallic": "#3CBED2",
        "dark_accent": "#14213B", "light_neutral": "#F2F5FA", "neutral": "#11131A"}),
    ("powder-blue", "Powder Blue", {
        "primary": "#A9C3DD", "secondary": "#D2E1EF", "metallic": "#D97969",
        "dark_accent": "#24384F", "light_neutral": "#FAFCFE", "neutral": "#26303A"}),
    ("modern-luxe", "Modern Luxe", {
        "primary": "#465A78", "secondary": "#9087B5", "metallic": "#C7C85D",
        "dark_accent": "#29384D", "light_neutral": "#F5F6F8", "neutral": "#25272B"}),
    ("periwinkle-stage", "Periwinkle Stage", {
        "primary": "#8D9CC7", "secondary": "#BBC3DE", "metallic": "#B44B71",
        "dark_accent": "#363C59", "light_neutral": "#F8F9FD", "neutral": "#272A38"}),
    ("art-deco-royal", "Art Deco Royal", {
        "primary": "#4B2E83", "secondary": "#7E5BA7", "metallic": "#2A8580",
        "dark_accent": "#2A132F", "light_neutral": "#F2E8D6", "neutral": "#0E0E12"}),
    ("lilac-premiere", "Lilac Premiere", {
        "primary": "#B39AC9", "secondary": "#D4C3E2", "metallic": "#357B61",
        "dark_accent": "#46304F", "light_neutral": "#FCF8FE", "neutral": "#302934"}),
    ("vintage-cinema", "Vintage Cinema", {
        "primary": "#5A1C24", "secondary": "#7A8048", "metallic": "#3C6685",
        "dark_accent": "#2B2019", "light_neutral": "#EFE4C8", "neutral": "#1A1817"}),
    ("rose-quartz", "Rose Quartz", {
        "primary": "#CFA4AE", "secondary": "#E9CDD3", "metallic": "#286A62",
        "dark_accent": "#56323B", "light_neutral": "#FFF8F9", "neutral": "#33282C"}),
    ("midnight-noir", "Midnight Noir", {
        "primary": "#222329", "secondary": "#626873", "metallic": "#C23B46",
        "dark_accent": "#0D0D10", "light_neutral": "#F4F3EF", "neutral": "#18191C"}),
    ("silver-screen", "Silver Screen", {
        "primary": "#B6B8BD", "secondary": "#D8D9DC", "metallic": "#486B94",
        "dark_accent": "#41444A", "light_neutral": "#FAFAFA", "neutral": "#202124"}),
]


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
