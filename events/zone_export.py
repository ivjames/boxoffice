"""Phase D of the seating-chart epic (docs/SEATING.md "D"): render a
performance's pricing-zone map to PNG or PDF, entirely server-side from
seat/zone DATA -- no browser/Chromium at runtime. PNG via Pillow (already a
dependency -- see Organization.logo); PDF via reportlab (pure-Python, no C
deps -- see the note added to requirements.txt).

Deviation from docs/SEATING.md's "D" sketch ("Reuse the already-installed
Chromium + Playwright ... to render the map view server-side ... no new
heavy dependency"): this phase's actual brief calls for a Chromium-free
renderer instead, for deployment robustness (the droplet runs plain pm2 +
nginx per docs/lab conventions -- no headless-browser process to keep
alive, no browser binary to install/update). Nothing in this repo actually
provisions Playwright/Chromium yet, so "reuse" would have meant adding a
new heavy dependency anyway; Pillow/reportlab are both pure-Python and
already-or-newly a plain `pip install`.

Coordinate math is NOT duplicated here: `events.zones.zone_map_geometry`
computes the exact same view-box/seat-radius numbers
dashboard.views.performance_pricing_zones turns into the live SVG editor's
`viewBox`, so the exported sheet can never visually drift from what staff
last saw on screen. Zone/price resolution reuses `events.pricing` too
(`zones_by_seat_id` for per-seat zone color, `orders.services.
price_tiers_by_section` for the section-default prices unzoned seats fall
back to) -- this module never re-derives a price or a color on its own.

Entry point: `render_zone_map(performance, *, fmt, size, labels, legend) ->
bytes`. Deterministic (same inputs -> same bytes) and read-only: it never
writes to the database, and every query it runs is scoped to
`performance.organization` via the helpers it calls into (zone_map_geometry
-> orders.services, zones_by_seat_id, price_tiers_by_section), so a caller
just needs to have already looked up `performance` scoped to the request's
organization -- exactly like every other manager-gated read in this
codebase.
"""

from __future__ import annotations

import io

from django.utils import timezone
from django.utils.dateformat import format as django_date_format

from .pricing import zones_by_seat_id
from .seat_contrast import text_color_hex, text_color_rgb
from .zones import zone_map_geometry

# -- shared constants ------------------------------------------------------

PAGE_SIZES_IN = {
    "letter": (8.5, 11.0),
    "legal": (8.5, 14.0),
}

PNG_DPI = 150

# Matches zone_editor.js's `seatFill()` fallback for an unzoned seat
# (`"#d1d5db"`) -- an unzoned seat should look the same on the exported
# sheet as it does live in the editor.
NEUTRAL_SEAT_HEX = "#d1d5db"
TITLE_HEX = "#111827"
MUTED_HEX = "#6b7280"
BORDER_HEX = "#d1d5db"


class ZoneExportError(Exception):
    """Bad `fmt`/`size` argument to render_zone_map. Message is safe to
    show directly to staff."""


def _hex_to_rgb(hex_color, default_hex=NEUTRAL_SEAT_HEX):
    value = hex_color or default_hex
    h = value.lstrip("#")
    if len(h) != 6:
        h = default_hex.lstrip("#")
    try:
        return tuple(int(h[i : i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        h = default_hex.lstrip("#")
        return tuple(int(h[i : i + 2], 16) for i in (0, 2, 4))


def _title_text(performance):
    return performance.event.title


def _subtitle_text(performance):
    starts_at = performance.starts_at
    if timezone.is_aware(starts_at):
        starts_at = timezone.localtime(starts_at)
    # Same format string templates/dashboard/zone_editor.html uses
    # (`{{ performance.starts_at|date:"D, M j Y - g:i A" }}`) so the export
    # header reads identically to what staff see on the editor page.
    when = django_date_format(starts_at, "D, M j Y - g:i A")
    return f"{when} - {performance.venue.name}"


def _fit_transform(view_box, box_w, box_h):
    """scale, offset_x, offset_y mapping seat-space (x, y) from `view_box`
    (min_x, min_y, width, height) into a `box_w` x `box_h` device rectangle
    whose origin is (0, 0) and whose y axis increases DOWNWARD (the same
    convention Seat.x/y and the SVG editor use), preserving aspect ratio and
    centering -- the same "xMidYMid meet" behavior the SVG's `viewBox`
    attribute gives the live editor."""
    min_x, min_y, vb_w, vb_h = view_box
    vb_w = vb_w or 1.0
    vb_h = vb_h or 1.0
    scale = min(box_w / vb_w, box_h / vb_h)
    drawn_w, drawn_h = vb_w * scale, vb_h * scale
    offset_x = (box_w - drawn_w) / 2 - min_x * scale
    offset_y = (box_h - drawn_h) / 2 - min_y * scale
    return scale, offset_x, offset_y


def _project(x, y, scale, offset_x, offset_y):
    return offset_x + x * scale, offset_y + y * scale


def _gather(performance):
    """Everything the PNG/PDF renderers need, computed once: seat geometry
    (shared with the live editor), each seat's zone (if any), one summary
    row per zone (name/color/price/seat count) for the legend, and one row
    per SECTION that still has unzoned seats with its default/override
    price (`orders.services.price_tiers_by_section` already implements the
    override-then-default rule) -- "also list the section/tier default
    prices for unzoned seats so the sheet is complete" per this phase's
    brief."""
    # Local import: orders.services -> events.pricing is already an
    # existing top-level dependency (orders depends on events); see
    # events.zones.clone_zones_from_performance for why this stays
    # call-time instead of a module-level import.
    from orders.services import price_tiers_by_section

    sections, seats, seat_radius, view_box = zone_map_geometry(performance)
    zone_by_seat = zones_by_seat_id(performance)

    zone_rows_by_id = {}
    for seat in seats:
        zone = zone_by_seat.get(seat.pk)
        if zone is None:
            continue
        row = zone_rows_by_id.setdefault(
            zone.pk, {"name": zone.name, "color": zone.color, "amount": zone.amount, "seat_count": 0}
        )
        row["seat_count"] += 1
    zone_rows = sorted(zone_rows_by_id.values(), key=lambda row: row["name"])

    tiers_by_section = price_tiers_by_section(performance)
    unzoned_section_rows = []
    seen_section_ids = set()
    for seat in seats:
        if seat.pk in zone_by_seat:
            continue
        section = seat.section
        if section.pk in seen_section_ids:
            continue
        seen_section_ids.add(section.pk)
        tier = tiers_by_section.get(section.pk)
        unzoned_section_rows.append(
            {"name": section.name, "amount": tier.amount if tier is not None else None}
        )
    unzoned_section_rows.sort(key=lambda row: row["name"])

    return {
        "sections": sections,
        "seats": seats,
        "seat_radius": seat_radius,
        "view_box": view_box,
        "zone_by_seat": zone_by_seat,
        "zone_rows": zone_rows,
        "unzoned_section_rows": unzoned_section_rows,
    }


def render_zone_map(performance, *, fmt="png", size="letter", labels=True, legend=True):
    """Render `performance`'s pricing-zone map to `fmt` ("png" or "pdf") at
    paper `size` ("letter" or "legal") and return the raw bytes.
    `labels`/`legend` toggle seat row/number labels and the zone/price
    legend -- both default on, per docs/SEATING.md's locked decision. A
    zoneless/empty performance (no zones applied yet, or no seats at all)
    still renders: unzoned seats draw in a neutral fill, and a chart-less
    performance draws an empty bordered box with a short note instead of
    erroring. Deterministic and read-only -- see this module's docstring."""
    fmt = (fmt or "").lower()
    size = (size or "").lower()
    if fmt not in ("png", "pdf"):
        raise ZoneExportError(f"Unsupported export format: {fmt!r} (expected 'png' or 'pdf').")
    if size not in PAGE_SIZES_IN:
        raise ZoneExportError(f"Unsupported paper size: {size!r} (expected 'letter' or 'legal').")

    data = _gather(performance)
    if fmt == "png":
        return _render_png(performance, data, size, labels, legend)
    return _render_pdf(performance, data, size, labels, legend)


# -- PNG (Pillow) -----------------------------------------------------------


def _render_png(performance, data, size, labels, legend):
    from PIL import Image, ImageDraw, ImageFont

    page_w_in, page_h_in = PAGE_SIZES_IN[size]
    width = int(round(page_w_in * PNG_DPI))
    height = int(round(page_h_in * PNG_DPI))
    img = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(img)

    margin = int(round(0.4 * PNG_DPI))
    gap = int(round(0.2 * PNG_DPI))

    title_font = ImageFont.load_default(size=22)
    subtitle_font = ImageFont.load_default(size=14)
    heading_font = ImageFont.load_default(size=15)
    row_font = ImageFont.load_default(size=12)

    title = _title_text(performance)
    draw.text((margin, margin), title, font=title_font, fill=TITLE_HEX)
    title_bottom = margin + draw.textbbox((0, 0), title, font=title_font)[3]

    subtitle = _subtitle_text(performance)
    draw.text((margin, title_bottom + 4), subtitle, font=subtitle_font, fill=MUTED_HEX)
    subtitle_bottom = title_bottom + 4 + draw.textbbox((0, 0), subtitle, font=subtitle_font)[3]

    top_y = subtitle_bottom + gap
    has_legend_content = bool(data["zone_rows"] or data["unzoned_section_rows"])
    legend_w = int(round(2.3 * PNG_DPI)) if (legend and has_legend_content) else 0
    legend_gap = gap if legend_w else 0

    map_x0, map_y0 = margin, top_y
    map_w = width - 2 * margin - legend_w - legend_gap
    map_h = height - top_y - margin

    if data["seats"]:
        scale, off_x, off_y = _fit_transform(data["view_box"], map_w, map_h)
        seat_r = max(2.0, data["seat_radius"] * scale)
        label_font = ImageFont.load_default(size=max(6, int(round(seat_r * 0.9))))
        for seat in data["seats"]:
            local_x, local_y = _project(seat.x, seat.y, scale, off_x, off_y)
            cx, cy = map_x0 + local_x, map_y0 + local_y
            zone = data["zone_by_seat"].get(seat.pk)
            fill = _hex_to_rgb(zone.color if zone is not None else None)
            draw.ellipse(
                [cx - seat_r, cy - seat_r, cx + seat_r, cy + seat_r], fill=fill, outline=(255, 255, 255)
            )
            if labels and seat_r >= 4:
                text = f"{seat.row_label}{seat.number}"
                bbox = draw.textbbox((0, 0), text, font=label_font)
                tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
                if tw <= seat_r * 2.6:
                    # WCAG contrast: black or white per the seat's fill so the
                    # label reads on a dark zone AND a pale one (seat_contrast).
                    draw.text(
                        (cx - tw / 2 - bbox[0], cy - th / 2 - bbox[1]),
                        text,
                        font=label_font,
                        fill=text_color_rgb(fill),
                    )
    else:
        draw.rectangle(
            [map_x0, map_y0, map_x0 + map_w, map_y0 + map_h], outline=_hex_to_rgb(BORDER_HEX)
        )
        draw.text(
            (map_x0 + 12, map_y0 + 12),
            "This performance has no seats yet.",
            font=subtitle_font,
            fill=MUTED_HEX,
        )

    if legend_w:
        _draw_png_legend(
            draw, data, map_x0 + map_w + legend_gap, map_y0, legend_w, heading_font, row_font
        )

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _draw_png_legend(draw, data, x0, y0, w, heading_font, row_font):
    swatch = 14
    line_h = swatch + 8
    y = y0

    draw.text((x0, y), "Zones", font=heading_font, fill=TITLE_HEX)
    y += 22
    if data["zone_rows"]:
        for row in data["zone_rows"]:
            draw.rectangle([x0, y, x0 + swatch, y + swatch], fill=_hex_to_rgb(row["color"]))
            plural = "" if row["seat_count"] == 1 else "s"
            text = f"{row['name']} - ${row['amount']} ({row['seat_count']} seat{plural})"
            draw.text((x0 + swatch + 8, y - 1), text, font=row_font, fill=TITLE_HEX)
            y += line_h
    else:
        draw.text((x0, y), "No zones applied yet.", font=row_font, fill=MUTED_HEX)
        y += line_h

    if data["unzoned_section_rows"]:
        y += 10
        draw.text((x0, y), "Unzoned (section default)", font=heading_font, fill=TITLE_HEX)
        y += 22
        for row in data["unzoned_section_rows"]:
            draw.rectangle([x0, y, x0 + swatch, y + swatch], fill=_hex_to_rgb(NEUTRAL_SEAT_HEX))
            price = f"${row['amount']}" if row["amount"] is not None else "no price set"
            text = f"{row['name']} - {price}"
            draw.text((x0 + swatch + 8, y - 1), text, font=row_font, fill=TITLE_HEX)
            y += line_h


# -- PDF (reportlab) ---------------------------------------------------------


def _render_pdf(performance, data, size, labels, legend):
    from reportlab.lib.colors import HexColor, black, white
    from reportlab.lib.pagesizes import legal as legal_size
    from reportlab.lib.pagesizes import letter as letter_size
    from reportlab.lib.units import inch
    from reportlab.pdfgen import canvas

    page_size = letter_size if size == "letter" else legal_size
    page_w, page_h = page_size

    buf = io.BytesIO()
    # invariant=1: suppress reportlab's default random document ID/
    # timestamp so render_zone_map is deterministic (same inputs -> same
    # bytes), matching the module docstring's promise and PNG's natural
    # determinism via Pillow.
    c = canvas.Canvas(buf, pagesize=page_size, invariant=1)

    margin = 0.4 * inch
    gap = 0.2 * inch

    c.setFillColor(HexColor(TITLE_HEX))
    c.setFont("Helvetica-Bold", 16)
    title_y = page_h - margin - 14
    c.drawString(margin, title_y, _title_text(performance))

    c.setFillColor(HexColor(MUTED_HEX))
    c.setFont("Helvetica", 10)
    subtitle_y = title_y - 16
    c.drawString(margin, subtitle_y, _subtitle_text(performance))
    c.setFillColor(black)

    top_y = subtitle_y - gap
    has_legend_content = bool(data["zone_rows"] or data["unzoned_section_rows"])
    legend_w = 2.3 * inch if (legend and has_legend_content) else 0
    legend_gap = gap if legend_w else 0

    map_w = page_w - 2 * margin - legend_w - legend_gap
    map_h = top_y - margin
    map_x0 = margin
    map_y0 = margin  # bottom of the map panel, in PDF (bottom-up) coordinates

    if data["seats"]:
        scale, off_x, off_y = _fit_transform(data["view_box"], map_w, map_h)
        seat_r = max(1.2, data["seat_radius"] * scale)
        font_size = max(4.0, seat_r * 1.1)
        for seat in data["seats"]:
            local_x, local_y_from_top = _project(seat.x, seat.y, scale, off_x, off_y)
            px = map_x0 + local_x
            py = map_y0 + (map_h - local_y_from_top)  # flip: device y-down -> PDF y-up
            zone = data["zone_by_seat"].get(seat.pk)
            seat_hex = zone.color if zone is not None else NEUTRAL_SEAT_HEX
            c.setFillColor(HexColor(seat_hex))
            c.circle(px, py, seat_r, fill=1, stroke=0)
            if labels and seat_r >= 3.5:
                text = f"{seat.row_label}{seat.number}"
                if font_size * 0.6 * len(text) <= seat_r * 2.4:
                    # WCAG contrast: black or white per the seat's fill so the
                    # label reads on a dark zone AND a pale one (seat_contrast).
                    c.setFillColor(HexColor(text_color_hex(_hex_to_rgb(seat_hex))))
                    c.setFont("Helvetica", font_size)
                    c.drawCentredString(px, py - font_size * 0.35, text)
        c.setFillColor(black)
    else:
        c.setStrokeColor(HexColor(BORDER_HEX))
        c.rect(map_x0, map_y0, map_w, map_h, fill=0, stroke=1)
        c.setFillColor(HexColor(MUTED_HEX))
        c.setFont("Helvetica", 10)
        c.drawString(map_x0 + 10, map_y0 + map_h - 16, "This performance has no seats yet.")
        c.setFillColor(black)

    if legend_w:
        _draw_pdf_legend(c, data, map_x0 + map_w + legend_gap, top_y)

    c.showPage()
    c.save()
    return buf.getvalue()


def _draw_pdf_legend(c, data, x0, top_y):
    from reportlab.lib.colors import HexColor, black

    swatch = 10
    line_h = 16
    y = top_y

    c.setFillColor(black)
    c.setFont("Helvetica-Bold", 12)
    c.drawString(x0, y, "Zones")
    y -= 18

    c.setFont("Helvetica", 9)
    if data["zone_rows"]:
        for row in data["zone_rows"]:
            c.setFillColor(HexColor(row["color"]))
            c.rect(x0, y - swatch + 2, swatch, swatch, fill=1, stroke=0)
            c.setFillColor(black)
            plural = "" if row["seat_count"] == 1 else "s"
            text = f"{row['name']} - ${row['amount']} ({row['seat_count']} seat{plural})"
            c.drawString(x0 + swatch + 6, y - swatch + 4, text)
            y -= line_h
    else:
        c.setFillColor(HexColor(MUTED_HEX))
        c.drawString(x0, y - 8, "No zones applied yet.")
        c.setFillColor(black)
        y -= line_h

    if data["unzoned_section_rows"]:
        y -= 8
        c.setFont("Helvetica-Bold", 12)
        c.drawString(x0, y, "Unzoned (section default)")
        y -= 18
        c.setFont("Helvetica", 9)
        for row in data["unzoned_section_rows"]:
            c.setFillColor(HexColor(NEUTRAL_SEAT_HEX))
            c.rect(x0, y - swatch + 2, swatch, swatch, fill=1, stroke=0)
            c.setFillColor(black)
            price = f"${row['amount']}" if row["amount"] is not None else "no price set"
            text = f"{row['name']} - {price}"
            c.drawString(x0 + swatch + 6, y - swatch + 4, text)
            y -= line_h
