"""Hold ("cart") service layer: the only place that mutates GA/reserved-seat
availability. Every mutation below is wrapped in `transaction.atomic()` and
re-reads availability from the DB inside that transaction before deciding —
see docs/ARCHITECTURE.md "Hold / cart lifecycle" and the Phase 2 handoff
availability rules this implements exactly:

- GA: available = GAAllocation.capacity - GAAllocation.sold
        - sum(quantity of active GA Holds for that performance)
      where active = expires_at > now.
- Reserved: a Seat is unavailable for a performance if it has a live Ticket
  (status != 'void') OR an active HoldSeat (parent Hold.expires_at > now)
  for that performance.

Locking: `select_for_update()` is used on the row(s) being contended for
(GAAllocation for GA, Seat rows for reserved) before re-checking. This is a
real row lock on Postgres. On SQLite it's a no-op, but
config.settings.base.harden_sqlite() sets transaction_mode=IMMEDIATE, so a
write transaction acquires the whole-database write lock at BEGIN — a second
concurrent call to one of the @transaction.atomic functions below simply
blocks until the first commits or rolls back, then re-reads committed state.
Net effect is the same on both backends: two overlapping attempts to grab
the same inventory serialize, and the second one's re-check correctly
rejects it if the first succeeded. select_for_update() is kept in the code
regardless, for Postgres parity (it does real work there; SQLite's
serialization is what makes the same guarantee hold today).
"""

from decimal import Decimal

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from events.models import GAAllocation, PriceTier
from venues.models import Seat

from .models import Hold, HoldSeat, Ticket, default_hold_expiry


class HoldError(Exception):
    """Base class for hold-service failures. The message is safe to show
    directly to the buyer (e.g. via the messages framework)."""


class InsufficientAvailabilityError(HoldError):
    """Not enough GA inventory left for the requested quantity."""


class SeatUnavailableError(HoldError):
    """One or more requested seats are ticketed or held by someone else."""


# --- session plumbing --------------------------------------------------


def get_session_key(request):
    """Return the current session's key, creating the session if this is
    its first write (request.session.session_key is None until something
    has been saved)."""
    if not request.session.session_key:
        request.session.create()
    return request.session.session_key


def get_active_hold(organization, performance, session_key):
    """The session's current unexpired Hold for this performance, if any."""
    return (
        Hold.objects.filter(
            organization=organization,
            performance=performance,
            session_key=session_key,
            expires_at__gt=timezone.now(),
        )
        .order_by("-created_at")
        .first()
    )


def _clear_session_hold(organization, performance, session_key):
    """Delete any Hold row(s) this session has for this performance
    (expired or not) — HoldSeat rows cascade-delete with them. Called before
    creating a replacement so a session only ever has one Hold per
    performance."""
    Hold.objects.filter(
        organization=organization, performance=performance, session_key=session_key
    ).delete()


# --- GA availability (read-only) ----------------------------------------


def _active_ga_held_qty(performance, *, exclude_session_key=None):
    qs = Hold.objects.filter(
        performance=performance, quantity__isnull=False, expires_at__gt=timezone.now()
    )
    if exclude_session_key is not None:
        qs = qs.exclude(session_key=exclude_session_key)
    return qs.aggregate(total=Sum("quantity"))["total"] or 0


def ga_available(performance, *, exclude_session_key=None):
    """GA seats sellable right now: capacity - sold - active held qty (the
    literal Phase 2 formula). `exclude_session_key`, when given, omits that
    session's own active hold from the "held" count — used on the selection
    page so a buyer editing their own quantity doesn't see availability
    reduced by their own prior pick.
    """
    try:
        allocation = performance.ga_allocation
    except GAAllocation.DoesNotExist:
        return 0
    held_qty = _active_ga_held_qty(performance, exclude_session_key=exclude_session_key)
    return max(allocation.capacity - allocation.sold - held_qty, 0)


# --- reserved-seat availability (read-only) -----------------------------


def get_seating_chart(performance):
    """Phase 3's resolution of "whichever chart is in use" (see the
    SeatingChart docstring in venues/models.py): Performance has no explicit
    chart FK, so we use the venue's first seating chart. Correct for the
    common one-chart-per-venue case (including the demo tenant). A venue
    that legitimately needs multiple charts in play will need an explicit
    Performance.seating_chart FK added in a later phase — flagged here
    rather than guessed at.
    """
    return performance.venue.seating_charts.order_by("pk").first()


def performance_seats(performance):
    """All bookable Seats for a RESERVED performance's seating chart."""
    chart = get_seating_chart(performance)
    if chart is None:
        return Seat.objects.none()
    return (
        Seat.objects.filter(organization=performance.organization_id, section__chart=chart)
        .select_related("section")
        .order_by("section__ordering", "row_label", "number")
    )


def price_tiers_by_section(performance):
    chart = get_seating_chart(performance)
    if chart is None:
        return {}
    tiers = PriceTier.objects.filter(organization=performance.organization_id, section__chart=chart)
    return {tier.section_id: tier for tier in tiers}


def reserved_seat_states(performance, session_key=None):
    """{seat_id: 'unavailable' | 'held_by_you' | 'available'} for every seat
    in the performance's chart. A seat is 'unavailable' if it backs a live
    (non-void) Ticket for this performance OR an active HoldSeat belonging
    to a different session; 'held_by_you' if the active HoldSeat is this
    session's own (still selectable — re-submitting keeps it); else
    'available'.
    """
    seats = performance_seats(performance)
    now = timezone.now()

    ticketed = set(
        Ticket.objects.filter(performance=performance)
        .exclude(status=Ticket.Status.VOID)
        .values_list("seat_id", flat=True)
    )

    held_by_you = set()
    held_by_others = set()
    holdseats = HoldSeat.objects.filter(
        hold__performance=performance, hold__expires_at__gt=now
    ).select_related("hold")
    for hs in holdseats:
        if session_key is not None and hs.hold.session_key == session_key:
            held_by_you.add(hs.seat_id)
        else:
            held_by_others.add(hs.seat_id)

    states = {}
    for seat in seats:
        if seat.id in ticketed or seat.id in held_by_others:
            states[seat.id] = "unavailable"
        elif seat.id in held_by_you:
            states[seat.id] = "held_by_you"
        else:
            states[seat.id] = "available"
    return states


def reserved_available_count(performance):
    states = reserved_seat_states(performance)
    return sum(1 for state in states.values() if state != "unavailable")


# --- mutations (transactional) ------------------------------------------


@transaction.atomic
def release_hold(*, organization, performance, session_key):
    """Delete the session's Hold (and its HoldSeats) for this performance."""
    _clear_session_hold(organization, performance, session_key)


@transaction.atomic
def release_hold_by_id(*, organization, session_key, hold_id):
    """Delete a specific Hold by pk, scoped to this org + session so a
    request can never release another session's or another tenant's hold."""
    if not hold_id:
        return
    Hold.objects.filter(organization=organization, session_key=session_key, pk=hold_id).delete()


@transaction.atomic
def set_ga_hold(*, organization, performance, session_key, user, price_tier, quantity):
    """Create/replace the session's GA hold for this performance with
    `quantity` seats at `price_tier`. quantity <= 0 releases instead.

    Locks the GAAllocation row (select_for_update), recomputes availability
    *excluding this session's own existing hold* (about to be replaced), and
    rejects if the requested quantity doesn't fit. See module docstring for
    why this is race-safe on both SQLite and Postgres.
    """
    if quantity is None or quantity <= 0:
        _clear_session_hold(organization, performance, session_key)
        return None

    allocation = GAAllocation.objects.select_for_update().get(performance=performance)

    other_held_qty = _active_ga_held_qty(performance, exclude_session_key=session_key)
    available = allocation.capacity - allocation.sold - other_held_qty
    if quantity > available:
        if available <= 0:
            raise InsufficientAvailabilityError("Sold out.")
        raise InsufficientAvailabilityError(
            f"Only {available} ticket(s) available for this performance."
        )

    _clear_session_hold(organization, performance, session_key)
    return Hold.objects.create(
        organization=organization,
        performance=performance,
        session_key=session_key,
        user=user,
        price_tier=price_tier,
        quantity=quantity,
        expires_at=default_hold_expiry(),
    )


@transaction.atomic
def set_reserved_hold(*, organization, performance, session_key, user, seat_ids):
    """Create/replace the session's reserved-seat hold for this performance
    with exactly `seat_ids` (the full desired selection — not a delta).
    Empty list releases instead.

    Locks the target Seat rows (select_for_update, ordered by pk so
    concurrent callers acquire locks in a consistent order and can't
    deadlock each other), then re-checks each seat against live Tickets and
    other sessions' active HoldSeats. If any requested seat is taken, the
    whole call fails with SeatUnavailableError naming the offending seats —
    nothing is partially held. See module docstring for the SQLite/Postgres
    locking parity note.
    """
    seat_ids = list(dict.fromkeys(int(s) for s in seat_ids))
    if not seat_ids:
        _clear_session_hold(organization, performance, session_key)
        return None

    chart = get_seating_chart(performance)
    seats = list(
        Seat.objects.select_for_update()
        .filter(organization=organization, section__chart=chart, pk__in=seat_ids)
        .select_related("section")
        .order_by("pk")
    )
    if len(seats) != len(seat_ids):
        raise SeatUnavailableError("One or more selected seats aren't part of this performance.")

    now = timezone.now()
    ticketed_ids = set(
        Ticket.objects.filter(performance=performance, seat_id__in=seat_ids)
        .exclude(status=Ticket.Status.VOID)
        .values_list("seat_id", flat=True)
    )
    held_by_others_ids = set(
        HoldSeat.objects.filter(seat_id__in=seat_ids, hold__expires_at__gt=now)
        .exclude(hold__session_key=session_key)
        .values_list("seat_id", flat=True)
    )
    unavailable_ids = ticketed_ids | held_by_others_ids
    if unavailable_ids:
        labels = ", ".join(f"{s.row_label}{s.number}" for s in seats if s.id in unavailable_ids)
        verb = "was" if len(unavailable_ids) == 1 else "were"
        raise SeatUnavailableError(
            f"Sorry — {labels} {verb} just taken by someone else. Please choose different seats."
        )

    tiers_by_section = price_tiers_by_section(performance)
    if any(seat.section_id not in tiers_by_section for seat in seats):
        raise HoldError("Some selected seats don't have a price set yet; contact the box office.")

    _clear_session_hold(organization, performance, session_key)
    hold = Hold.objects.create(
        organization=organization,
        performance=performance,
        session_key=session_key,
        user=user,
        expires_at=default_hold_expiry(),
    )
    HoldSeat.objects.bulk_create(
        [
            HoldSeat(
                organization=organization,
                hold=hold,
                seat=seat,
                price_tier=tiers_by_section[seat.section_id],
            )
            for seat in seats
        ]
    )
    return hold


def hold_total(hold):
    """Dollar total for a Hold: quantity * tier for GA, sum of per-seat
    tiers for reserved."""
    if hold.price_tier_id and hold.quantity:
        return hold.price_tier.amount * hold.quantity
    total = Decimal("0.00")
    for hold_seat in hold.hold_seats.select_related("price_tier").all():
        total += hold_seat.price_tier.amount
    return total
