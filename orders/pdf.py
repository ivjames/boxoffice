"""Server-side PDF of an order's tickets, for the "Download" affordance on
the tickets page (templates/orders/ticket_detail.html) and the guest portal.

Rendered entirely from order/ticket DATA with reportlab (already a
dependency -- see events/zone_export.py and requirements.txt) plus segno for
the QR image, so there's no browser/Chromium at runtime, consistent with the
rest of this codebase's export story. One ticket per "card": event / time /
venue header, then each ticket's seat (or "General Admission") and its scan
QR -- the same signed QR the confirmation page and email show (orders.qr /
orders.tokens), so a printed PDF scans at the door identically.

Entry point: render_order_pdf(order) -> bytes. Needs no request: the QR
encodes a bare "<token>.<sig>" ticket code (orders.tokens.scan_code), not a
URL, so there's no host/scheme to resolve.
"""

import io

from events.timezones import in_venue_tz
from tenants.logo_images import read_logo_bytes

from .qr import ticket_qr_png_bytes


def render_order_pdf(order):
    """Return the PDF bytes for `order`'s tickets. `order` must already be
    tenant-scoped by the caller (it comes off the unguessable Order.token
    lookup in orders.views.order_pdf). Reads only; touches no DB state beyond
    the tickets it iterates."""
    from reportlab.lib.colors import HexColor, black
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.units import inch
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas

    page_w, page_h = letter
    margin = 0.6 * inch

    buf = io.BytesIO()
    # invariant=1: deterministic output (no random doc id/timestamp), matching
    # zone_export's PDF path.
    c = canvas.Canvas(buf, pagesize=letter, invariant=1)

    performance = order.performance
    tickets = list(
        order.tickets.select_related("seat", "seat__section").order_by(
            "seat__section__ordering", "seat__row_label", "seat__number", "id"
        )
    )

    # Read the org logo once: it brands the header (below) AND is centered on
    # each ticket's QR (see the card loop). None => an unbranded PDF + plain QRs.
    logo_bytes = read_logo_bytes(order.organization)

    # --- Theater logo (first page, top-right) ---
    # Brand the downloadable/printed ticket with the org's logo when it has one,
    # opposite the event title. Drawn inside a fixed box with preserveAspectRatio
    # so any logo shape fits without distortion, and mask="auto" so a transparent
    # PNG (the normalized/background-removed form) has no white plate. Wrapped
    # like the QR below: a bad logo must never sink the whole PDF.
    if logo_bytes:
        logo_w, logo_h = 1.6 * inch, 0.55 * inch
        try:
            c.drawImage(
                ImageReader(io.BytesIO(logo_bytes)),
                page_w - margin - logo_w,
                page_h - margin - logo_h,
                width=logo_w,
                height=logo_h,
                preserveAspectRatio=True,
                anchor="ne",
                mask="auto",
            )
        except Exception:
            pass

    # --- Order header (first page) ---
    c.setFillColor(black)
    c.setFont("Helvetica-Bold", 18)
    y = page_h - margin
    c.drawString(margin, y, _truncate(performance.event.title, 60))

    c.setFont("Helvetica", 11)
    y -= 22
    c.drawString(
        margin,
        y,
        in_venue_tz(performance.starts_at, performance.venue.timezone).strftime("%a, %b %-d %Y — %-I:%M %p"),
    )
    y -= 16
    c.drawString(margin, y, performance.venue.name)
    y -= 16
    c.setFillColor(HexColor("#6b7280"))
    c.drawString(margin, y, f"{order.organization.name} · Order {order.token}")

    # --- One card per ticket ---
    qr_size = 1.6 * inch
    card_h = qr_size + 0.5 * inch
    y -= 0.5 * inch

    for index, ticket in enumerate(tickets, start=1):
        # New page if this card wouldn't fit above the bottom margin.
        if y - card_h < margin:
            c.showPage()
            y = page_h - margin

        qr_top = y
        qr_x = margin
        try:
            c.drawImage(
                ImageReader(io.BytesIO(ticket_qr_png_bytes(ticket, logo_bytes=logo_bytes))),
                qr_x,
                qr_top - qr_size,
                width=qr_size,
                height=qr_size,
                preserveAspectRatio=True,
                mask="auto",
            )
        except Exception:
            # A QR that can't render must not sink the whole PDF -- leave a
            # gap and keep going; the ticket's token text below still lets
            # the door look it up manually.
            pass

        text_x = qr_x + qr_size + 0.3 * inch
        ty = qr_top - 0.2 * inch
        c.setFillColor(black)
        c.setFont("Helvetica-Bold", 13)
        c.drawString(text_x, ty, f"Ticket {index}")

        ty -= 18
        c.setFont("Helvetica", 11)
        c.drawString(text_x, ty, _seat_label(ticket))

        ty -= 16
        c.setFillColor(HexColor("#6b7280"))
        c.setFont("Helvetica", 9)
        c.drawString(text_x, ty, f"Status: {ticket.get_status_display()}")
        ty -= 13
        c.drawString(text_x, ty, str(ticket.token))

        y = qr_top - card_h

    c.showPage()
    c.save()
    return buf.getvalue()


def _seat_label(ticket):
    seat = ticket.seat
    if seat is None:
        return "General Admission"
    return f"{seat.section.name} — Row {seat.row_label}, Seat {seat.number}"


def _truncate(text, limit):
    text = text or ""
    return text if len(text) <= limit else text[: limit - 1] + "…"
