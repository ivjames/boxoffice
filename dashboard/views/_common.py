# --- shared helpers used by more than one dashboard.views submodule -------
#
# _section_color/_SECTION_PALETTE are used by both venues.py (the chart
# editor's section create/render) and zones.py (the pricing-zone editor's
# seat coloring) -- see docs/EDITOR.md -- so they live here rather than in
# either submodule to avoid a circular import between the two.

_SECTION_PALETTE = [
    "#e11d48", "#2563eb", "#059669", "#d97706", "#7c3aed",
    "#0891b2", "#db2777", "#65a30d", "#4f46e5", "#dc2626",
]


def _section_color(index):
    return _SECTION_PALETTE[index % len(_SECTION_PALETTE)]
