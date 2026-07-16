"""Small display helpers for the branding page (templates/dashboard/
branding.html), which needs to look up a color by a *role key* that only
exists as a loop variable at render time -- something the built-in template
language can't do (no `dict[var]` / `getattr(obj, var)`).

Both are read-only lookups used purely to render swatches; neither touches
state.
"""

from django import template

register = template.Library()


@register.filter
def dictkey(mapping, key):
    """`mapping[key]` for a dict whose key is a template variable -- e.g.
    `current_palette|dictkey:role` where `role` is the loop's role key."""
    try:
        return mapping.get(key, "")
    except AttributeError:
        return ""


@register.filter
def attr(obj, name):
    """`getattr(obj, name)` for an attribute named by a template variable --
    e.g. `scheme|attr:role` to read a ColorScheme's per-role color field."""
    return getattr(obj, name, "")
