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

from django.db import transaction
from django.utils import timezone

from orders.models import Ticket
from orders.tokens import verify_ticket_sig


@dataclass
class ScanResult:
    ok: bool
    reason: str  # "ok" | "bad_sig" | "not_found" | "already_used" | "void"
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
            when = timezone.localtime(ticket.used_at).strftime("%b %d, %Y %I:%M %p") if ticket.used_at else "an earlier scan"
            return ScanResult(
                ok=False,
                reason="already_used",
                ticket=ticket,
                message=f"Already scanned {when} by {who}.",
            )

        # status == VALID -> admit, flip to used.
        ticket.status = Ticket.Status.USED
        ticket.used_at = timezone.now()
        ticket.scanned_by = scanned_by
        ticket.save(update_fields=["status", "used_at", "scanned_by"])
        return ScanResult(ok=True, reason="ok", ticket=ticket, message="Valid ticket — admit.")
