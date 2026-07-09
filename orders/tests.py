from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone

from events.models import Event, GAAllocation, Performance, PriceTier
from orders.models import Hold, HoldSeat, Order, OrderItem, Payment, Ticket, default_hold_expiry
from venues.models import Seat, SeatingChart, Section, Venue
from venues.tests import make_org

User = get_user_model()


class OrdersFixtureMixin:
    def build_ga_performance(self):
        self.org = make_org("roxy")
        self.venue = Venue.objects.create(organization=self.org, name="Main Stage")
        self.event = Event.objects.create(organization=self.org, title="Show", slug="show")
        self.performance = Performance.objects.create(
            organization=self.org,
            event=self.event,
            venue=self.venue,
            starts_at=timezone.now(),
            seating_mode=Performance.SeatingMode.GA,
        )
        GAAllocation.objects.create(organization=self.org, performance=self.performance, capacity=100)
        self.price_tier = PriceTier.objects.create(
            organization=self.org, performance=self.performance, name="GA", amount=Decimal("35.00")
        )

    def build_reserved_performance(self):
        self.org = make_org("roxy")
        self.venue = Venue.objects.create(organization=self.org, name="Main Stage")
        self.event = Event.objects.create(organization=self.org, title="Show", slug="show")
        self.performance = Performance.objects.create(
            organization=self.org,
            event=self.event,
            venue=self.venue,
            starts_at=timezone.now(),
            seating_mode=Performance.SeatingMode.RESERVED,
        )
        chart = SeatingChart.objects.create(organization=self.org, venue=self.venue, name="Standard")
        self.section = Section.objects.create(organization=self.org, chart=chart, name="Orchestra")
        self.seat = Seat.objects.create(organization=self.org, section=self.section, row_label="A", number="1")
        self.price_tier = PriceTier.objects.create(
            organization=self.org, section=self.section, name="Orchestra", amount=Decimal("65.00")
        )


class HoldModelTests(OrdersFixtureMixin, TestCase):
    def test_ga_hold(self):
        self.build_ga_performance()
        hold = Hold.objects.create(
            organization=self.org,
            performance=self.performance,
            session_key="sess-1",
            price_tier=self.price_tier,
            quantity=2,
        )
        self.assertGreater(hold.expires_at, timezone.now())
        self.assertEqual(hold.quantity, 2)

    def test_default_hold_expiry_is_about_ten_minutes_out(self):
        before = timezone.now()
        expiry = default_hold_expiry()
        after = timezone.now()
        self.assertGreaterEqual((expiry - before).total_seconds(), 9 * 60)
        self.assertLessEqual((expiry - after).total_seconds(), 11 * 60)

    def test_reserved_hold_via_holdseat_through(self):
        self.build_reserved_performance()
        hold = Hold.objects.create(
            organization=self.org, performance=self.performance, session_key="sess-2"
        )
        HoldSeat.objects.create(
            organization=self.org, hold=hold, seat=self.seat, price_tier=self.price_tier
        )
        self.assertEqual(list(hold.seats.all()), [self.seat])

    def test_hold_ga_fields_must_be_set_together(self):
        self.build_ga_performance()
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Hold.objects.create(
                    organization=self.org,
                    performance=self.performance,
                    session_key="sess-3",
                    quantity=2,
                    price_tier=None,
                )

    def test_duplicate_seat_on_same_hold_rejected(self):
        self.build_reserved_performance()
        hold = Hold.objects.create(
            organization=self.org, performance=self.performance, session_key="sess-4"
        )
        HoldSeat.objects.create(organization=self.org, hold=hold, seat=self.seat, price_tier=self.price_tier)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                HoldSeat.objects.create(
                    organization=self.org, hold=hold, seat=self.seat, price_tier=self.price_tier
                )


class OrderTicketModelTests(OrdersFixtureMixin, TestCase):
    def test_create_order_with_items_and_tickets(self):
        self.build_reserved_performance()
        order = Order.objects.create(
            organization=self.org,
            performance=self.performance,
            buyer_email="buyer@example.com",
            buyer_name="Buyer Person",
            total=Decimal("65.00"),
        )
        OrderItem.objects.create(
            organization=self.org,
            order=order,
            price_tier=self.price_tier,
            seat=self.seat,
            quantity=1,
            unit_amount=Decimal("65.00"),
        )
        ticket = Ticket.objects.create(
            organization=self.org,
            order=order,
            performance=self.performance,
            seat=self.seat,
            holder_name="Buyer Person",
        )
        Payment.objects.create(
            organization=self.org, order=order, amount=Decimal("65.00"), status="succeeded"
        )

        self.assertEqual(order.items.count(), 1)
        self.assertEqual(order.tickets.count(), 1)
        self.assertEqual(order.payments.count(), 1)
        self.assertEqual(ticket.status, Ticket.Status.VALID)
        self.assertIsNotNone(order.token)

    def test_order_token_is_unique(self):
        self.build_ga_performance()
        order_a = Order.objects.create(
            organization=self.org, performance=self.performance, buyer_email="a@example.com", total=Decimal("10.00")
        )
        order_b = Order.objects.create(
            organization=self.org, performance=self.performance, buyer_email="b@example.com", total=Decimal("10.00")
        )
        self.assertNotEqual(order_a.token, order_b.token)

    def test_only_one_live_ticket_per_seat_per_performance(self):
        self.build_reserved_performance()
        order = Order.objects.create(
            organization=self.org, performance=self.performance, buyer_email="a@example.com", total=Decimal("65.00")
        )
        Ticket.objects.create(
            organization=self.org, order=order, performance=self.performance, seat=self.seat
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Ticket.objects.create(
                    organization=self.org, order=order, performance=self.performance, seat=self.seat
                )

    def test_void_ticket_does_not_block_reissue(self):
        self.build_reserved_performance()
        order = Order.objects.create(
            organization=self.org, performance=self.performance, buyer_email="a@example.com", total=Decimal("65.00")
        )
        voided = Ticket.objects.create(
            organization=self.org,
            order=order,
            performance=self.performance,
            seat=self.seat,
            status=Ticket.Status.VOID,
        )
        # A new valid ticket for the same seat is allowed once the old one is void.
        reissued = Ticket.objects.create(
            organization=self.org, order=order, performance=self.performance, seat=self.seat
        )
        self.assertNotEqual(voided.pk, reissued.pk)

    def test_ga_ticket_has_no_seat(self):
        self.build_ga_performance()
        order = Order.objects.create(
            organization=self.org, performance=self.performance, buyer_email="a@example.com", total=Decimal("35.00")
        )
        ticket = Ticket.objects.create(
            organization=self.org, order=order, performance=self.performance, seat=None
        )
        self.assertIsNone(ticket.seat)

    def test_ticket_scanned_by_user(self):
        self.build_ga_performance()
        scanner = User.objects.create_user(email="scanner@example.com", password="pw")
        order = Order.objects.create(
            organization=self.org, performance=self.performance, buyer_email="a@example.com", total=Decimal("35.00")
        )
        ticket = Ticket.objects.create(
            organization=self.org,
            order=order,
            performance=self.performance,
            status=Ticket.Status.USED,
            used_at=timezone.now(),
            scanned_by=scanner,
        )
        self.assertEqual(ticket.scanned_by, scanner)
