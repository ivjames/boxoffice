"""QR code rendering for ticket scan codes. The code encoded into the QR (a
compact '<token>.<sig>' string, no URL) is defined in orders/tokens.py; this
module is the segno wrapper, plus an optional centered-logo overlay.

The logo overlay is safe because every ticket QR is generated at error="h"
(~30% error correction): a small mark centered on a white pad obscures well
under that budget, and the finder patterns in the three corners are never
touched. It's applied only when the ticket's org has a logo, and any failure
falls back to the plain QR -- a scannable code always wins over a branded one.
"""

import base64
import io

import segno

from tenants.logo_images import read_logo_bytes

from .tokens import scan_code

# Longest edge of the centered logo as a fraction of the QR's rendered width.
# Kept modest so the obscured area (the plate, a touch larger than the logo)
# stays well inside the error="h" ~30% budget and the code scans reliably.
# Verified by decoding the composited output in orders/test_qr.py.
LOGO_FRACTION = 0.22

# Sentinel so callers that already hold the org's logo bytes (a whole order's
# tickets share one logo -- read it once, not per ticket) can pass them in,
# while a bare call still resolves the logo itself. Distinct from None, which
# means "this org has no logo".
_UNSET = object()


def _compose_qr_with_logo(qr, logo_bytes, *, scale, border):
    """Render `qr` to a PIL image and center the logo on a white pad over it.
    Returns optimized PNG bytes. Raises on any imaging error -- the caller
    falls back to the plain QR."""
    from PIL import Image

    buf = io.BytesIO()
    qr.save(buf, kind="png", scale=scale, border=border)
    buf.seek(0)
    code = Image.open(buf).convert("RGBA")

    logo = Image.open(io.BytesIO(logo_bytes)).convert("RGBA")
    target = max(1, int(code.width * LOGO_FRACTION))
    logo.thumbnail((target, target), Image.LANCZOS)

    # A white pad behind the logo turns the covered center into one clean island
    # (a local quiet zone) instead of ambiguous half-modules -- this is what
    # keeps a transparent or busy logo scannable.
    pad = max(2, target // 8)
    plate = Image.new("RGBA", (logo.width + pad * 2, logo.height + pad * 2), (255, 255, 255, 255))
    plate.alpha_composite(logo, (pad, pad))

    pos = ((code.width - plate.width) // 2, (code.height - plate.height) // 2)
    code.alpha_composite(plate, pos)

    out = io.BytesIO()
    code.save(out, format="PNG", optimize=True)
    return out.getvalue()


def ticket_qr_png_bytes(ticket, scale=6, border=2, logo_bytes=_UNSET):
    """PNG bytes of a Ticket's signed scan QR, with the org logo centered when
    it has one. `logo_bytes` lets a caller iterating a whole order's tickets read
    the shared logo once and pass it to each; omit it and the logo is resolved
    from the ticket's org."""
    if logo_bytes is _UNSET:
        logo_bytes = read_logo_bytes(ticket.organization)

    qr = segno.make(scan_code(ticket), error="h")
    if logo_bytes:
        try:
            return _compose_qr_with_logo(qr, logo_bytes, scale=scale, border=border)
        except Exception:
            pass  # fall back to the plain QR below
    buf = io.BytesIO()
    qr.save(buf, kind="png", scale=scale, border=border)
    return buf.getvalue()


def ticket_qr_data_uri(ticket, scale=5, border=2, logo_bytes=_UNSET):
    """PNG data URI for a Ticket's scan QR code -- embeddable directly as
    <img src="..."> on the ticket confirmation page and inline in the ticket
    email (both need a self-contained image with no extra request; a data
    URI is the simplest thing that works in both an HTML page and most mail
    clients).

    `error="h"` (~30% error correction, the highest level) lets the code
    still scan with up to ~30% of its area obscured/damaged -- a scuffed or
    partially torn printout, a smudge, a phone photographing it at an angle,
    or the centered org logo (see ticket_qr_png_bytes). That level stays
    affordable because the QR encodes only the ticket code
    (orders/tokens.scan_code) -- a 10-char token + truncated HMAC, ~31 chars
    in QR alphanumeric mode -- rather than a full scan URL."""
    png = ticket_qr_png_bytes(ticket, scale=scale, border=border, logo_bytes=logo_bytes)
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")
