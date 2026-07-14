"""Signed, expiring magic-link tokens for guest (ticket-buyer) sign-in.

Guests have no password (see guests.models.GuestAccount) -- they prove they
own an email address by clicking a link we emailed to it. This module is the
single source of truth for that token scheme, deliberately self-contained
(imports nothing from guests.views/services) so the signing format lives in
exactly one place.

Scheme: django.core.signing.dumps({"gid": <guest pk>, "org": <org pk>}) with
a per-purpose salt. The signature is keyed on settings.SECRET_KEY, so a
token can't be forged without it; the embedded org id is re-checked on
verify against the tenant the link was opened on, so a token minted for one
theater can't be redeemed on another even if subdomains ever came to share a
cookie. Tokens expire (default 24h) via signing's built-in `max_age`."""

from django.core import signing

# Namespacing salt: keeps these tokens from ever validating against another
# signing use of the same SECRET_KEY (Django recommends a distinct salt per
# purpose).
_SALT = "guests.magic-link"

# How long a sign-in link stays valid. Long enough to survive a slow inbox /
# a link opened the next morning, short enough that a forwarded/leaked old
# email can't be replayed indefinitely.
DEFAULT_MAX_AGE_SECONDS = 24 * 60 * 60


def make_login_token(guest):
    """Signed token that identifies `guest` (by pk + org pk). Opaque and
    tamper-evident; carries no email in the clear."""
    return signing.dumps({"gid": guest.pk, "org": guest.organization_id}, salt=_SALT)


def read_login_token(token, organization, *, max_age=DEFAULT_MAX_AGE_SECONDS):
    """Return the guest pk encoded in `token` iff it's a valid, unexpired
    signature AND its embedded org id matches `organization`. Returns None
    for anything wrong -- bad signature, expired, malformed, or minted for a
    different tenant -- so callers never have to distinguish the failure
    modes (they all mean "send them back to request a fresh link")."""
    try:
        data = signing.loads(token, salt=_SALT, max_age=max_age)
    except signing.BadSignature:
        return None
    if not isinstance(data, dict):
        return None
    if str(data.get("org")) != str(organization.pk):
        return None
    return data.get("gid")


# --- One-click marketing unsubscribe -------------------------------------
#
# A campaign email carries an unsubscribe link the recipient can click WITHOUT
# signing in (they may be reading in a mail client that never touches our
# session). The link must therefore be self-authenticating and unforgeable --
# the exact same signed-payload trick the sign-in magic link uses -- but with
# two deliberate differences from make_login_token above:
#
#   * a DISTINCT salt, so an unsubscribe link can never be replayed as a
#     sign-in token (or vice versa) even though both sign {gid, org} with the
#     same SECRET_KEY (Django's per-purpose-salt guidance);
#   * NO expiry by default. A sign-in link is short-lived on purpose (a leaked
#     old email mustn't grant account access); an unsubscribe link is the
#     OPPOSITE -- it must keep working however stale the email is, because a
#     recipient acting on a months-old newsletter to opt out is exactly who we
#     most want to honor (and CAN-SPAM/GDPR expect that link to stay live). So
#     read_unsubscribe_token defaults max_age=None; a caller may still pass one.
_UNSUB_SALT = "guests.unsubscribe"


def make_unsubscribe_token(guest):
    """Signed token identifying `guest` (by pk + org pk) for the one-click
    unsubscribe link embedded in every campaign email. Opaque and tamper-
    evident like the sign-in token, but salted for unsubscribe only so the two
    can't be interchanged."""
    return signing.dumps(
        {"gid": guest.pk, "org": guest.organization_id}, salt=_UNSUB_SALT
    )


def read_unsubscribe_token(token, organization, *, max_age=None):
    """Return the guest pk encoded in `token` iff it's a valid signature whose
    embedded org id matches `organization`, else None -- the unsubscribe twin
    of read_login_token, with the identical org re-check (a token minted for
    one theater can't opt a guest out on another). Defaults to NO expiry
    (max_age=None), unlike the sign-in reader: an unsubscribe link must honor a
    recipient however old the email is (see _UNSUB_SALT's note). Returns None
    for any failure mode so the view can uniformly treat it as "bad/again"."""
    try:
        data = signing.loads(token, salt=_UNSUB_SALT, max_age=max_age)
    except signing.BadSignature:
        return None
    if not isinstance(data, dict):
        return None
    if str(data.get("org")) != str(organization.pk):
        return None
    return data.get("gid")
