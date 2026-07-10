"""HMAC signing for ticket QR payloads.

A Ticket's QR encodes just a compact code -- NOT a URL:

    <TICKET.TOKEN>.<SIG>

The in-page scanner (static/js/scanner.js) reads that code directly and calls
the redeem endpoint itself (scanning/views.scan_redeem), which verifies `sig`
before honoring a scan. Encoding only the bits it must -- no scheme, host, or
path -- roughly halves the QR's module count versus embedding a full URL.
(The trade-off, made deliberately: a phone's stock camera app can no longer
open the code, so redemption goes exclusively through the staff scanner UI.)

This module is the single source of truth for the scheme on both sides, so
it's deliberately self-contained: it imports nothing from orders.services or
orders.views, and the scanner imports ONLY this module (plus the Ticket
model) rather than reaching into the checkout/webhook code.

Shape of the code, all chosen to keep the QR sparse:

- sig = base32(HMAC-SHA256(key, str(token))[:12]) -- the HMAC is computed
  full-width over SHA-256, then TRUNCATED to its first 96 bits, which are
  base32-encoded (20 uppercase chars, no padding). Truncating an HMAC is a
  standard, sound construction (cf. RFC 4226/HOTP); 96 bits is far more than
  a forger needs to fail against here, because `key` is secret and
  per-tenant AND a valid signature is only one of three gates -- the scanner
  still has to be a logged-in staffer with the scanner role at that org.

- token (orders.models.new_token) and sig are BOTH uppercase base32 (A-Z2-7)
  joined by '.', so the whole code sits inside QR "alphanumeric mode" (~45%
  denser per module than byte mode). '.' can't occur inside either half, so
  the split back into (token, sig) is unambiguous.

`key` is derived from settings.SECRET_KEY + the ticket's organization_id.
Per-tenant (not global) so a leaked signature for one theater's tickets
can't forge another's. Deliberately NOT derived from the org's Stripe keys:
QR signing must keep working for orgs that haven't configured Stripe yet
(e.g. comp tickets issued directly) and must stay stable if Stripe keys are
rotated.
"""

import base64
import hashlib
import hmac

# Bytes of the HMAC-SHA256 digest kept in the signature. 12 -> 96-bit sig.
_SIG_BYTES = 12


def _key_for_org(organization_id):
    from django.conf import settings

    return f"{settings.SECRET_KEY}:ticket-qr:{organization_id}".encode()


def sign_token(token, organization_id):
    """Raw signer: base32 of the first 96 bits of HMAC-SHA256 over `token`
    (str/UUID), scoped to `organization_id`. Lower-level than sign_ticket()
    so the scanner can verify against a token pulled straight off the URL
    without first loading the Ticket row (a fast-fail before hitting the
    DB). Uppercase base32 (not base64url) so the signature stays inside QR
    alphanumeric mode -- see the module docstring."""
    digest = hmac.new(_key_for_org(organization_id), str(token).encode(), hashlib.sha256).digest()
    return base64.b32encode(digest[:_SIG_BYTES]).rstrip(b"=").decode()


def sign_ticket(ticket):
    """HMAC signature for a persisted Ticket, using its own token + org."""
    return sign_token(ticket.token, ticket.organization_id)


def verify_ticket_sig(token, sig, organization_id):
    """Constant-time check that `sig` is the correct signature for `token`
    under `organization_id`. Never raises -- a missing/malformed/empty `sig`
    (e.g. someone hand-editing the QR URL) just returns False."""
    if not sig:
        return False
    expected = sign_token(token, organization_id)
    return hmac.compare_digest(expected, sig)


def scan_code(ticket):
    """The compact '<token>.<sig>' string a Ticket's QR encodes -- no URL,
    scheme, or host (see module docstring). The in-page scanner decodes this,
    splits on '.', and calls the redeem endpoint itself. Needs no `request`:
    there's nothing host- or scheme-dependent left to build."""
    return f"{ticket.token}.{sign_ticket(ticket)}"
