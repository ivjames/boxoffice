"""Ticket redemption: the one place a Ticket flips valid -> used. See
docs/ARCHITECTURE.md "Ticket & scanning" and orders/tokens.py (signature
scheme, reused verbatim -- this module does not reinvent it).

Concurrency: redeem_ticket() runs entirely inside transaction.atomic() and
takes select_for_update() on the Ticket row before checking/flipping status.
On SQLite, config.settings.base.harden_sqlite() has already set
transaction_mode=IMMEDIATE, so a second concurrent call's BEGIN blocks until
the first COMMITs, then re-reads committed state -- same
lock-then-recheck-then-mutate pattern orders/services.py and
payments/services.py use for hold/seat contention. Two staff members
scanning the same physical ticket at the same instant: the one whose
transaction commits first flips it to 'used'; the second's re-read inside
its own lock sees 'used' already and returns an already_used ScanResult.
Exactly one of them can ever get `ok=True`.
"""

from dataclasses import dataclass
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from events.timezones import in_venue_tz
from orders.models import Ticket
from orders.tokens import verify_ticket_sig

# Scanning is time-gated to around showtime: staff admit from an hour before
# the performance starts through the show itself. A genuine, unused ticket
# scanned well before doors -- or long after the show -- is a strong signal of
# a wrong-day/wrong-time mix-up (someone at the door with tomorrow's ticket, or
# last night's), so we surface it as an amber "wrong time" result that explains
# the mismatch instead of silently admitting. The ticket is NOT flipped to used
# in that case: it stays VALID so it can be re-scanned at the right time (and so
# staff can still make a judgment call). Performance carries no end time, so the
# close side is a generous show-length grace past starts_at rather than an exact
# curtain-down.
SCAN_WINDOW_OPENS_BEFORE = timedelta(hours=1)
SCAN_WINDOW_CLOSES_AFTER = timedelta(hours=4)


@dataclass
class ScanResult:
    ok: bool
    reason: str  # "ok" | "bad_sig" | "not_found" | "already_used" | "void" | "wrong_time"
    message: str
    ticket: Ticket | None = None

    def as_dict(self):
        ticket = self.ticket
        return {
            "ok": self.ok,
            "reason": self.reason,
            "message": self.message,
            "ticket": None
            if ticket is None
            else {
                "token": str(ticket.token),
                "holder_name": ticket.holder_name,
                "seat": f"{ticket.seat.row_label}{ticket.seat.number}" if ticket.seat_id else None,
                "performance": str(ticket.performance),
                "status": ticket.status,
                "used_at": ticket.used_at.isoformat() if ticket.used_at else None,
                "scanned_by": ticket.scanned_by.email if ticket.scanned_by_id else None,
            },
        }


def redeem_ticket(*, organization, token, sig, scanned_by):
    """Verify + (if valid) redeem the ticket identified by `token` for
    `organization`. Never raises for "normal" failure modes (bad sig, not
    found, already used, void) -- those come back as a non-ok ScanResult so
    the view can render a clear PASS/FAIL either way.
    """
    if not verify_ticket_sig(token, sig, organization.id):
        return ScanResult(ok=False, reason="bad_sig", message="Invalid or missing signature.")

    with transaction.atomic():
        ticket = (
            Ticket.objects.select_for_update()
            .select_related(
                "order",
                "performance",
                "performance__event",
                "performance__venue",
                "seat",
                "seat__section",
                "scanned_by",
            )
            .filter(organization=organization, token=token)
            .first()
        )
        if ticket is None:
            return ScanResult(ok=False, reason="not_found", message="No ticket found for this code.")

        if ticket.status == Ticket.Status.VOID:
            return ScanResult(
                ok=False, reason="void", ticket=ticket, message="This ticket has been voided."
            )

        if ticket.status == Ticket.Status.USED:
            who = ticket.scanned_by.email if ticket.scanned_by_id else "unknown staff"
            when = (
                in_venue_tz(ticket.used_at, ticket.performance.venue.timezone).strftime("%b %d, %Y %I:%M %p")
                if ticket.used_at
                else "an earlier scan"
            )
            return ScanResult(
                ok=False,
                reason="already_used",
                ticket=ticket,
                message=f"Already scanned {when} by {who}.",
            )

        # Ticket is genuine and unused -- but only admit it around showtime.
        # Checked after void/already_used so those verdicts still take
        # precedence, and before the flip so a wrong-time scan leaves the
        # ticket VALID for a later, in-window re-scan.
        wrong_time = _wrong_time_result(ticket)
        if wrong_time is not None:
            return wrong_time

        # status == VALID and within the scan window -> admit, flip to used.
        ticket.status = Ticket.Status.USED
        ticket.used_at = timezone.now()
        ticket.scanned_by = scanned_by
        ticket.save(update_fields=["status", "used_at", "scanned_by"])
        return ScanResult(ok=True, reason="ok", ticket=ticket, message="Valid ticket — admit.")


def _wrong_time_result(ticket):
    """Return an amber `wrong_time` ScanResult if `now` is outside this
    performance's scan window, else None. The message names the actual
    performance time so door staff can see at a glance which show the ticket
    is really for.
    """
    starts_at = ticket.performance.starts_at
    now = timezone.now()
    opens_at = starts_at - SCAN_WINDOW_OPENS_BEFORE
    closes_at = starts_at + SCAN_WINDOW_CLOSES_AFTER
    when = in_venue_tz(starts_at, ticket.performance.venue.timezone).strftime("%b %d, %Y %I:%M %p")

    if now < opens_at:
        return ScanResult(
            ok=False,
            reason="wrong_time",
            ticket=ticket,
            message=(
                f"Too early — this ticket is for {when}. "
                "Scanning opens an hour before the show."
            ),
        )
    if now > closes_at:
        return ScanResult(
            ok=False,
            reason="wrong_time",
            ticket=ticket,
            message=f"Too late — this ticket was for {when}. The scan window has closed.",
        )
    return None
