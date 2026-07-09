"""QR code rendering for ticket scan links. The signing scheme encoded into
the URL lives in orders/tokens.py; this module is just the segno wrapper."""

import segno

from .tokens import build_ticket_scan_url


def ticket_qr_data_uri(ticket, request, scale=5, border=2):
    """PNG data URI for a Ticket's scan QR code -- embeddable directly as
    <img src="..."> on the ticket confirmation page and inline in the ticket
    email (both need a self-contained image with no extra request; a data
    URI is the simplest thing that works in both an HTML page and most mail
    clients). `error="m"` (~15% error correction) gives some tolerance for a
    scuffed printout without bloating the code."""
    url = build_ticket_scan_url(ticket, request)
    return segno.make(url, error="m").png_data_uri(scale=scale, border=border)
