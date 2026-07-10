"""QR code rendering for ticket scan codes. The code encoded into the QR (a
compact '<token>.<sig>' string, no URL) is defined in orders/tokens.py; this
module is just the segno wrapper."""

import segno

from .tokens import scan_code


def ticket_qr_data_uri(ticket, scale=5, border=2):
    """PNG data URI for a Ticket's scan QR code -- embeddable directly as
    <img src="..."> on the ticket confirmation page and inline in the ticket
    email (both need a self-contained image with no extra request; a data
    URI is the simplest thing that works in both an HTML page and most mail
    clients).

    `error="h"` (~30% error correction, the highest level) lets the code
    still scan with up to ~30% of its area obscured/damaged -- a scuffed or
    partially torn printout, a smudge, a phone photographing it at an angle.
    That level stays affordable because the QR encodes only the ticket code
    (orders/tokens.scan_code) -- token + truncated HMAC, ~36 chars in QR
    alphanumeric mode -- rather than a full scan URL."""
    return segno.make(scan_code(ticket), error="h").png_data_uri(scale=scale, border=border)
