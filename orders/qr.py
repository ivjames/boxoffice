"""QR code rendering for ticket scan links. The signing scheme encoded into
the URL lives in orders/tokens.py; this module is just the segno wrapper."""

import segno

from .tokens import build_ticket_scan_url


def ticket_qr_data_uri(ticket, request, scale=5, border=2):
    """PNG data URI for a Ticket's scan QR code -- embeddable directly as
    <img src="..."> on the ticket confirmation page and inline in the ticket
    email (both need a self-contained image with no extra request; a data
    URI is the simplest thing that works in both an HTML page and most mail
    clients).

    `error="h"` (~30% error correction, the highest level) lets the code
    still scan with up to ~30% of its area obscured/damaged -- a scuffed or
    partially torn printout, a smudge, a phone photographing it at an angle.
    That level used to bloat the QR; it's affordable now because the encoded
    URL is much shorter -- a 12-char token (orders/models.new_token) plus a
    22-char signature (orders/tokens.py) instead of a 36-char UUID plus a
    64-char hex HMAC -- so raising error correction here is a net shrink."""
    url = build_ticket_scan_url(ticket, request)
    return segno.make(url, error="h").png_data_uri(scale=scale, border=border)
