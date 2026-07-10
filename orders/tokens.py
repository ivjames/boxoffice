"""HMAC signing for ticket QR payloads.

Tickets carry a QR that encodes an absolute URL:

    HTTPS://<TENANT-SUBDOMAIN>/S/<TICKET.TOKEN>/<SIG>/

The scanner endpoint (scanning/views.scan_redeem) verifies that `sig` before
honoring a scan. This module is the single source of truth for that scheme
on both sides, so it's deliberately self-contained: it imports nothing from
orders.services or orders.views, and the scanner imports ONLY this module
(plus the Ticket model) rather than reaching into the checkout/webhook code.

Everything about this scheme is shaped to keep the QR sparse:

- sig = base32(HMAC-SHA256(key, str(token))[:12]) -- the HMAC is computed
  full-width over SHA-256, then TRUNCATED to its first 96 bits, which are
  base32-encoded (20 uppercase chars, no padding). Truncating an HMAC is a
  standard, sound construction (cf. RFC 4226/HOTP); 96 bits is far more than
  a forger needs to fail against here, because `key` is secret and
  per-tenant AND a valid signature is only one of three gates -- the scanner
  still has to be a logged-in staffer with the scanner role at that org.

- The signature is a PATH SEGMENT (/S/<token>/<sig>/), not a ?sig= query
  param: '?' and '=' fall outside QR "alphanumeric mode", and staying in
  that mode (vs byte mode) is what packs the code ~45% denser per module.
  The token (orders.models.new_token) is base32 for the same reason, and
  build_ticket_scan_url() uppercases the whole URL to finish the job.

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

from django.urls import reverse

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


def scan_path(ticket):
    """Path (no scheme/host) of the redeem endpoint for this ticket: the
    token and signature as consecutive path segments (see module docstring
    for why they're path segments and not a ?sig= query param). Built via
    reverse() off the `scan_redeem` route so the exact shape lives in one
    place (scanning/urls.py); the scanner reuses this to build the same
    path."""
    return reverse("scan_redeem", args=[ticket.token, sign_ticket(ticket)])


def build_ticket_scan_url(ticket, request):
    """Absolute URL for this ticket's QR code, built off `request` (the
    tenant-subdomain request rendering the ticket page or handling the
    Stripe webhook) so it comes out correct in dev
    (HTTP://ROXY.LOCALHOST:8000/S/...) and in prod
    (HTTPS://ROXY.LAB980.COM/S/...) without this module needing to know
    BASE_DOMAIN or guess a scheme.

    .upper() is the last step of keeping the QR in alphanumeric mode: the
    token and signature are already uppercase base32 and the path is /S/, so
    uppercasing scheme + host (both case-insensitive to the browser and the
    tenant router, which lowercases the Host header again) leaves the whole
    string within QR's uppercase-only alphanumeric charset -- nothing left to
    force byte mode."""
    return request.build_absolute_uri(scan_path(ticket)).upper()
