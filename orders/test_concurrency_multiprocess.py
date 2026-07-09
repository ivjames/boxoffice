"""Real multiprocess concurrency test: proves the "no double-booking"
guarantee against the ACTUAL production database backend, not pytest's
in-memory SQLite.

Why this module exists (see orders/test_services.py's module docstring for
the full explanation of the shortcut it takes): Django's default test runner
gives each SQLite test connection an in-memory `:memory:` database, and every
new connection to `:memory:` is its OWN empty database. Real background
threads/processes opening their own connections during a test would each see
an empty DB, not the shared state a real race needs -- so the existing
concurrency tests instead assert "given the first call's committed state,
the second call is rejected", which is *logically* the same interleaving
`harden_sqlite()` guarantees, but never actually exercises multiple live
connections contending for the SQLite file lock at the same time.

This module closes that gap for real:

1. Migrates a real ON-DISK SQLite file (a temp path, not `:memory:`) and
   seeds it with exactly one free reserved seat and a GA performance with
   exactly one ticket of headroom left.
2. Spawns N (>=8) real OS processes via `multiprocessing` using the "spawn"
   start method (NOT the default "fork" on Linux -- fork would inherit this
   test process's already-`django.setup()`-configured settings, which point
   at pytest-django's in-memory test database, plus any open DB file
   descriptors; spawn gives each worker a genuinely fresh interpreter that
   sets its own DATABASE_URL and calls `django.setup()` itself, exactly like
   a real gunicorn worker process would).
3. Every worker blocks on a `multiprocessing.Barrier` so they all attempt to
   hold-then-convert the same last seat / last GA ticket at (as close to)
   the same instant as the OS scheduler allows -- a true race between
   independent connections to the same file, not a simulated one.
4. Asserts EXACTLY ONE process wins (gets a real Ticket), every other
   process fails cleanly with the expected HoldError/FulfillmentError
   subtype (no unhandled exception leaks out of a worker), and a final
   read against the shared file confirms inventory never went negative or
   over capacity.

This is what actually validates harden_sqlite()'s transaction_mode=IMMEDIATE
serialization end to end. It's slower than the rest of the suite (spawning
~2N fresh Python/Django interpreters) but still runs in a few seconds -- it
is intentionally NOT skipped by default. It carries the custom
`multiprocess_concurrency` marker (registered in pytest.ini) purely so it
CAN be deselected in a hurry with `pytest -m "not multiprocess_concurrency"`;
a normal `pytest` invocation runs it like any other test.

A second, optional test at the bottom of this module exercises the same
race against real Postgres (`select_for_update()` row locking instead of
SQLite's whole-database IMMEDIATE lock) when a `POSTGRES_URL` env var is
set, and is skipped cleanly otherwise.
"""

import json
import multiprocessing
import os
import time
from decimal import Decimal
from pathlib import Path

import pytest

pytestmark = pytest.mark.multiprocess_concurrency

N_WORKERS = 10
BARRIER_TIMEOUT = 30
JOIN_TIMEOUT = 90


# --- Django bootstrap for spawned children -------------------------------


def _bootstrap_django(database_url):
    """Point a FRESH interpreter at the shared database and boot Django
    exactly the way manage.py/gunicorn would. Must only ever run inside a
    `multiprocessing` "spawn" child -- see the module docstring for why fork
    would defeat the entire point of this test."""
    os.environ["DATABASE_URL"] = database_url
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")
    import django

    django.setup()


# --- seeding (runs once, in its own subprocess) ---------------------------


def _seed(database_url, result_path, subdomain):
    _bootstrap_django(database_url)
    from django.core.management import call_command
    from django.utils import timezone

    call_command("migrate", run_syncdb=True, verbosity=0)

    from events.models import Event, GAAllocation, Performance, PriceTier
    from tenants.models import Organization
    from venues.models import Seat, SeatingChart, Section, Venue

    org = Organization.objects.create(
        name="Race Theater",
        slug=subdomain,
        subdomain=subdomain,
        contact_email=f"box@{subdomain}.example.com",
    )
    venue = Venue.objects.create(organization=org, name="Race Stage")
    event = Event.objects.create(organization=org, title="Race Show", slug="race-show")

    # RESERVED performance with exactly ONE free seat.
    reserved_perf = Performance.objects.create(
        organization=org,
        event=event,
        venue=venue,
        starts_at=timezone.now(),
        seating_mode=Performance.SeatingMode.RESERVED,
    )
    chart = SeatingChart.objects.create(organization=org, venue=venue, name="Standard")
    section = Section.objects.create(organization=org, chart=chart, name="Orchestra")
    seat = Seat.objects.create(organization=org, section=section, row_label="A", number="1")
    PriceTier.objects.create(
        organization=org, section=section, name="Orchestra", amount=Decimal("50.00")
    )

    # GA performance with capacity that leaves exactly ONE ticket of headroom.
    ga_perf = Performance.objects.create(
        organization=org,
        event=event,
        venue=venue,
        starts_at=timezone.now(),
        seating_mode=Performance.SeatingMode.GA,
    )
    GAAllocation.objects.create(organization=org, performance=ga_perf, capacity=10, sold=9)
    PriceTier.objects.create(organization=org, performance=ga_perf, name="GA", amount=Decimal("20.00"))

    Path(result_path).write_text(
        json.dumps(
            {
                "org_id": org.pk,
                "reserved_performance_id": reserved_perf.pk,
                "seat_id": seat.pk,
                "ga_performance_id": ga_perf.pk,
            }
        )
    )


# --- workers: race for the last reserved seat ------------------------------


def _write_outcome(results_dir, worker_id, outcome):
    Path(results_dir, f"{worker_id}.json").write_text(json.dumps(outcome))


def _race_for_seat(worker_id, database_url, results_dir, barrier, org_id, performance_id, seat_id):
    """Hold-then-convert the last reserved seat. Every branch is caught and
    recorded -- nothing may leak out of a worker process unhandled, per the
    task's "no exceptions leaking" requirement."""
    outcome = {"worker_id": worker_id, "won": False, "error": None}
    try:
        _bootstrap_django(database_url)
        from events.models import Performance
        from orders import services as order_services
        from payments import services as payment_services
        from tenants.models import Organization

        org = Organization.objects.get(pk=org_id)
        performance = Performance.objects.get(pk=performance_id)

        try:
            barrier.wait(timeout=BARRIER_TIMEOUT)
        except Exception as exc:  # BrokenBarrierError or a sibling timing out
            outcome["error"] = f"barrier:{exc!r}"
            _write_outcome(results_dir, worker_id, outcome)
            return

        try:
            hold = order_services.set_reserved_hold(
                organization=org,
                performance=performance,
                session_key=f"race-seat-{worker_id}",
                user=None,
                seat_ids=[seat_id],
            )
            session = {
                "id": f"cs_race_seat_{worker_id}",
                "payment_intent": f"pi_race_seat_{worker_id}",
                "metadata": {"hold_id": str(hold.pk), "organization_id": str(org.pk)},
                "customer_details": {"email": f"racer{worker_id}@example.com", "name": f"Racer {worker_id}"},
            }
            order, created = payment_services.fulfill_checkout_session(org, session)
            outcome["won"] = True
            outcome["order_id"] = order.pk
        except order_services.HoldError as exc:
            outcome["error"] = f"hold_rejected:{exc}"
        except payment_services.FulfillmentError as exc:
            outcome["error"] = f"fulfillment_rejected:{exc}"
    except Exception as exc:  # noqa: BLE001 -- must never crash the worker silently
        outcome["error"] = f"unexpected:{exc!r}"
    _write_outcome(results_dir, worker_id, outcome)


def _race_for_ga_ticket(worker_id, database_url, results_dir, barrier, org_id, performance_id):
    """Hold-then-convert the last GA ticket (quantity=1 each, capacity has
    exactly 1 slot of headroom)."""
    outcome = {"worker_id": worker_id, "won": False, "error": None}
    try:
        _bootstrap_django(database_url)
        from events.models import Performance, PriceTier
        from orders import services as order_services
        from payments import services as payment_services
        from tenants.models import Organization

        org = Organization.objects.get(pk=org_id)
        performance = Performance.objects.get(pk=performance_id)
        tier = PriceTier.objects.get(organization=org, performance=performance)

        try:
            barrier.wait(timeout=BARRIER_TIMEOUT)
        except Exception as exc:
            outcome["error"] = f"barrier:{exc!r}"
            _write_outcome(results_dir, worker_id, outcome)
            return

        try:
            hold = order_services.set_ga_hold(
                organization=org,
                performance=performance,
                session_key=f"race-ga-{worker_id}",
                user=None,
                price_tier=tier,
                quantity=1,
            )
            session = {
                "id": f"cs_race_ga_{worker_id}",
                "payment_intent": f"pi_race_ga_{worker_id}",
                "metadata": {"hold_id": str(hold.pk), "organization_id": str(org.pk)},
                "customer_details": {"email": f"racer{worker_id}@example.com", "name": f"Racer {worker_id}"},
            }
            order, created = payment_services.fulfill_checkout_session(org, session)
            outcome["won"] = True
            outcome["order_id"] = order.pk
        except order_services.HoldError as exc:
            outcome["error"] = f"hold_rejected:{exc}"
        except payment_services.FulfillmentError as exc:
            outcome["error"] = f"fulfillment_rejected:{exc}"
    except Exception as exc:  # noqa: BLE001
        outcome["error"] = f"unexpected:{exc!r}"
    _write_outcome(results_dir, worker_id, outcome)


# --- verification (runs once, in its own subprocess, after the race) ------


def _verify_reserved(database_url, result_path, performance_id, seat_id):
    _bootstrap_django(database_url)
    from orders.models import Ticket

    live = (
        Ticket.objects.filter(performance_id=performance_id, seat_id=seat_id)
        .exclude(status=Ticket.Status.VOID)
        .count()
    )
    Path(result_path).write_text(json.dumps({"live_ticket_count": live}))


def _verify_ga(database_url, result_path, performance_id):
    _bootstrap_django(database_url)
    from events.models import GAAllocation
    from orders.models import Ticket

    allocation = GAAllocation.objects.get(performance_id=performance_id)
    ticket_count = Ticket.objects.filter(performance_id=performance_id).count()
    Path(result_path).write_text(
        json.dumps(
            {
                "sold": allocation.sold,
                "capacity": allocation.capacity,
                "ticket_count": ticket_count,
            }
        )
    )


# --- process orchestration helpers -----------------------------------------


def _spawn_and_wait(ctx, target, args, timeout=JOIN_TIMEOUT):
    proc = ctx.Process(target=target, args=args)
    proc.start()
    proc.join(timeout=timeout)
    if proc.is_alive():
        proc.terminate()
        proc.join(5)
        raise AssertionError(f"subprocess for {target.__name__} hung past {timeout}s")
    if proc.exitcode != 0:
        raise AssertionError(f"subprocess for {target.__name__} exited with code {proc.exitcode}")


def _run_race(ctx, target, database_url, results_dir, worker_args):
    """Start N_WORKERS copies of `target`, each called as
    `target(worker_id, database_url, str(results_dir), barrier, *worker_args)`,
    all sharing one Barrier so they race as close to simultaneously as the
    OS scheduler allows."""
    barrier = ctx.Barrier(N_WORKERS)
    procs = [
        ctx.Process(
            target=target,
            args=(worker_id, database_url, str(results_dir), barrier, *worker_args),
        )
        for worker_id in range(N_WORKERS)
    ]
    for proc in procs:
        proc.start()

    deadline = time.monotonic() + JOIN_TIMEOUT
    for proc in procs:
        proc.join(timeout=max(0, deadline - time.monotonic()))
    still_alive = [proc for proc in procs if proc.is_alive()]
    for proc in still_alive:
        proc.terminate()

    outcomes = []
    for worker_id in range(N_WORKERS):
        result_file = Path(results_dir, f"{worker_id}.json")
        if result_file.exists():
            outcomes.append(json.loads(result_file.read_text()))
        else:
            outcomes.append(
                {"worker_id": worker_id, "won": False, "error": "no_result_file (process hung/killed)"}
            )
    return outcomes, still_alive


def _assert_single_clean_winner(outcomes, still_alive, expected_error_prefix):
    assert not still_alive, (
        f"{len(still_alive)} worker process(es) never finished within {JOIN_TIMEOUT}s "
        f"(hung) -- outcomes so far: {outcomes}"
    )
    unexpected = [o for o in outcomes if o["error"] and o["error"].startswith("unexpected")]
    assert not unexpected, f"worker(s) raised an unhandled exception: {unexpected}"
    barrier_broke = [o for o in outcomes if o["error"] and o["error"].startswith("barrier")]
    assert not barrier_broke, f"worker(s) failed to synchronize at the barrier: {barrier_broke}"

    winners = [o for o in outcomes if o["won"]]
    assert len(winners) == 1, f"expected exactly one winner, got {len(winners)}: {outcomes}"

    losers = [o for o in outcomes if not o["won"]]
    assert len(losers) == N_WORKERS - 1
    for loser in losers:
        assert loser["error"] and loser["error"].startswith(expected_error_prefix), (
            f"loser failed for the wrong reason: {loser}"
        )


# --- the tests --------------------------------------------------------------


@pytest.fixture(scope="module")
def seeded_sqlite_db(tmp_path_factory):
    """Migrates + seeds ONE real on-disk SQLite file, shared by both races
    below (cuts the migrate+seed cost from 2x to 1x). Seeding runs in its
    own spawned subprocess so it never touches this test process's own
    (pytest-django, in-memory) Django configuration."""
    tmp_path = tmp_path_factory.mktemp("multiprocess_concurrency")
    db_path = tmp_path / "race.sqlite3"
    database_url = f"sqlite:///{db_path}"
    result_path = tmp_path / "seed_result.json"

    ctx = multiprocessing.get_context("spawn")
    _spawn_and_wait(ctx, _seed, (database_url, str(result_path), "race-sqlite"))

    seed = json.loads(result_path.read_text())
    seed["database_url"] = database_url
    seed["tmp_path"] = tmp_path
    return seed


def test_real_multiprocess_reserved_seat_single_winner(seeded_sqlite_db):
    """N=10 real OS processes race, via a shared Barrier, to hold-then-
    convert the SAME single free seat against a real on-disk SQLite file.
    Exactly one must win a Ticket; every other process must fail cleanly.
    """
    seed = seeded_sqlite_db
    results_dir = seed["tmp_path"] / "results_seat"
    results_dir.mkdir()
    ctx = multiprocessing.get_context("spawn")

    start = time.monotonic()
    outcomes, still_alive = _run_race(
        ctx,
        _race_for_seat,
        seed["database_url"],
        results_dir,
        (seed["org_id"], seed["reserved_performance_id"], seed["seat_id"]),
    )
    elapsed = time.monotonic() - start
    print(f"\n[multiprocess seat race] {N_WORKERS} processes, {elapsed:.2f}s wall time")

    _assert_single_clean_winner(outcomes, still_alive, expected_error_prefix="hold_rejected")

    verify_path = seed["tmp_path"] / "verify_seat.json"
    _spawn_and_wait(
        ctx,
        _verify_reserved,
        (seed["database_url"], str(verify_path), seed["reserved_performance_id"], seed["seat_id"]),
    )
    verified = json.loads(verify_path.read_text())
    assert verified["live_ticket_count"] == 1, (
        f"expected exactly one live Ticket for the seat, found {verified['live_ticket_count']} "
        "-- double-booking!"
    )


def test_real_multiprocess_ga_ticket_single_winner(seeded_sqlite_db):
    """Same shape as the reserved-seat race, but for the last unit of GA
    capacity: capacity=10, sold=9 going in, so only ONE quantity=1 hold can
    ever fit. N=10 processes race for it."""
    seed = seeded_sqlite_db
    results_dir = seed["tmp_path"] / "results_ga"
    results_dir.mkdir()
    ctx = multiprocessing.get_context("spawn")

    start = time.monotonic()
    outcomes, still_alive = _run_race(
        ctx,
        _race_for_ga_ticket,
        seed["database_url"],
        results_dir,
        (seed["org_id"], seed["ga_performance_id"]),
    )
    elapsed = time.monotonic() - start
    print(f"\n[multiprocess GA race] {N_WORKERS} processes, {elapsed:.2f}s wall time")

    _assert_single_clean_winner(outcomes, still_alive, expected_error_prefix="hold_rejected")

    verify_path = seed["tmp_path"] / "verify_ga.json"
    _spawn_and_wait(ctx, _verify_ga, (seed["database_url"], str(verify_path), seed["ga_performance_id"]))
    verified = json.loads(verify_path.read_text())
    assert verified["sold"] == 10, f"expected sold to go from 9 to exactly 10, got {verified['sold']}"
    assert verified["sold"] <= verified["capacity"], "oversold past capacity!"
    assert verified["ticket_count"] == 1, (
        f"expected exactly one Ticket created for the GA race, found {verified['ticket_count']}"
    )


# --- optional Postgres variant: real row locking via select_for_update ----


POSTGRES_URL = os.environ.get("POSTGRES_URL")


@pytest.mark.skipif(not POSTGRES_URL, reason="POSTGRES_URL not set -- skipping the Postgres concurrency variant")
def test_real_multiprocess_reserved_seat_single_winner_postgres(tmp_path_factory):
    """Identical race to test_real_multiprocess_reserved_seat_single_winner,
    but against a real Postgres database (POSTGRES_URL, e.g.
    postgres://user:pass@localhost:5432/boxoffice_concurrency_test) instead
    of SQLite. orders/services.py's select_for_update() calls are no-ops on
    SQLite but do real row locking on Postgres -- this is what actually
    exercises that code path. The seed/worker/verify functions above are
    fully backend-agnostic (they only call django.setup() against whatever
    DATABASE_URL they're given), so this reuses them unchanged, just pointed
    at Postgres. Uses a random subdomain per run so repeated runs against a
    long-lived Postgres instance don't collide on the unique constraint, and
    cleans up its own Organization (cascades to everything) afterward.
    """
    import uuid

    subdomain = f"race-pg-{uuid.uuid4().hex[:12]}"
    tmp_path = tmp_path_factory.mktemp("multiprocess_concurrency_pg")
    result_path = tmp_path / "seed_result.json"
    ctx = multiprocessing.get_context("spawn")

    _spawn_and_wait(ctx, _seed, (POSTGRES_URL, str(result_path), subdomain))
    seed = json.loads(result_path.read_text())

    results_dir = tmp_path / "results_seat"
    results_dir.mkdir()
    outcomes, still_alive = _run_race(
        ctx,
        _race_for_seat,
        POSTGRES_URL,
        results_dir,
        (seed["org_id"], seed["reserved_performance_id"], seed["seat_id"]),
    )
    try:
        _assert_single_clean_winner(outcomes, still_alive, expected_error_prefix="hold_rejected")

        verify_path = tmp_path / "verify_seat.json"
        _spawn_and_wait(
            ctx,
            _verify_reserved,
            (POSTGRES_URL, str(verify_path), seed["reserved_performance_id"], seed["seat_id"]),
        )
        verified = json.loads(verify_path.read_text())
        assert verified["live_ticket_count"] == 1
    finally:
        _spawn_and_wait(ctx, _cleanup_org, (POSTGRES_URL, seed["org_id"]))


def _cleanup_org(database_url, org_id):
    _bootstrap_django(database_url)
    from tenants.models import Organization

    Organization.objects.filter(pk=org_id).delete()
