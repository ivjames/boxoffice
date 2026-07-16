# --- shared helpers used by more than one dashboard.views submodule -------
#
# _section_color/_SECTION_PALETTE are used by both venues.py (the chart
# editor's section create/render) and zones.py (the pricing-zone editor's
# seat coloring) -- see docs/EDITOR.md -- so they live here rather than in
# either submodule to avoid a circular import between the two.

import csv

from django.http import HttpResponse


def csv_response(filename, header, rows):
    """A downloadable text/csv HttpResponse: `header` is the column-title row,
    `rows` an iterable of row sequences. Centralizes the response + disposition
    + writer boilerplate the dashboard's CSV exports (donations, passes,
    audience) each repeated."""
    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    writer = csv.writer(response)
    writer.writerow(header)
    writer.writerows(rows)
    return response

_SECTION_PALETTE = [
    "#e11d48", "#2563eb", "#059669", "#d97706", "#7c3aed",
    "#0891b2", "#db2777", "#65a30d", "#4f46e5", "#dc2626",
]


def _section_color(index):
    return _SECTION_PALETTE[index % len(_SECTION_PALETTE)]
