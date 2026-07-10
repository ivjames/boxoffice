"""HMAC signing for ticket QR payloads.

Phase 4 issues tickets whose QR encodes an absolute URL:

    https://<tenant-subdomain>/scan/redeem/<ticket.token>/?sig=<hmac>

Phase 5's scanner endpoint (not built yet) verifies that `sig` before
honoring a scan. This module is the single source of truth for that scheme
on both sides, so it's deliberately self-contained: it imports nothing from
orders.services or orders.views, and Phase 5 should import ONLY this module
(plus the Ticket model) rather than reaching into the rest of Phase 4's
checkout/webhook code.

Scheme: sig = base64url(HMAC-SHA256(key, str(token))[:16]) -- the HMAC is
computed full-width over SHA-256 and then TRUNCATED to its first 128 bits,
which are base64url-encoded (22 chars, no padding) rather than hex-encoded
(the full digest would be 64 hex chars). Truncating an HMAC is a standard,
sound construction (cf. RFC 4226/HOTP), and 128 bits is far more than a
forger needs to fail against here: `key` is secret and per-tenant, and even
a valid signature is only one of three gates -- the scanner still has to be
a logged-in staffer with the scanner role at that org. The payoff is a much
shorter QR URL, which lets orders/qr.py raise the error-correction level.

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

# Bytes of the HMAC-SHA256 digest kept in the signature. 16 -> 128-bit sig.
_SIG_BYTES = 16


def _key_for_org(organization_id):
    from django.conf import settings

    return f"{settings.SECRET_KEY}:ticket-qr:{organization_id}".encode()


def sign_token(token, organization_id):
    """Raw signer: base64url of the first 128 bits of HMAC-SHA256 over
    `token` (str/UUID), scoped to `organization_id`. Lower-level than
    sign_ticket() so the scanner can verify against a token pulled straight
    off the URL without first loading the Ticket row (a fast-fail before
    hitting the DB)."""
    digest = hmac.new(_key_for_org(organization_id), str(token).encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest[:_SIG_BYTES]).rstrip(b"=").decode()


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


def scan_path(ticket):
    """Path (no scheme/host) of Phase 5's redeem endpoint for this ticket,
    with its signature attached as a query param. Kept separate from
    build_ticket_scan_url() so Phase 5's scanner can reuse the exact same
    path-building without duplicating the query-string logic."""
    return f"/scan/redeem/{ticket.token}/?sig={sign_ticket(ticket)}"


def build_ticket_scan_url(ticket, request):
    """Absolute URL for this ticket's QR code, built off `request` (the
    tenant-subdomain request rendering the ticket page or handling the
    Stripe webhook) so it comes out correct in dev
    (http://roxy.localhost:8000/scan/redeem/...) and in prod
    (https://roxy.lab980.com/scan/redeem/...) without this module needing to
    know BASE_DOMAIN or guess a scheme."""
    return request.build_absolute_uri(scan_path(ticket))
