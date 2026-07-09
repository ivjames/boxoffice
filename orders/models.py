import uuid
from datetime import timedelta

from django.conf import settings
from django.db import models
from django.utils import timezone

from events.models import Performance, PriceTier
from tenants.models import TenantScopedModel
from venues.models import Seat


def default_hold_expiry():
    """Named function (not a lambda) so migrations can serialize this default.
    Business logic for extending/refreshing holds lives in Phase 3."""
    return timezone.now() + timedelta(minutes=10)


class Hold(TenantScopedModel):
    """A temporary reservation of inventory while a buyer is mid-checkout.
    GA holds set `price_tier` + `quantity`; reserved-seat holds attach Seats
    via the HoldSeat through model instead. Expiry/availability math is
    Phase 3 (storefront) — this app only carries the shape of the data.
    """

    performance = models.ForeignKey(Performance, on_delete=models.CASCADE, related_name="holds")
    session_key = models.CharField(max_length=64)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="holds",
    )
    expires_at = models.DateTimeField(default=default_hold_expiry)

    # GA selection.
    price_tier = models.ForeignKey(
        PriceTier, on_delete=models.CASCADE, null=True, blank=True, related_name="holds"
    )
    quantity = models.PositiveIntegerField(null=True, blank=True)

    # Reserved-seat selection.
    seats = models.ManyToManyField(Seat, through="HoldSeat", related_name="holds", blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta(TenantScopedModel.Meta):
        indexes = TenantScopedModel.Meta.indexes + [
            models.Index(fields=["organization", "performance"]),
            models.Index(fields=["performance", "expires_at"]),
            models.Index(fields=["session_key"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(quantity__isnull=True, price_tier__isnull=True)
                    | models.Q(quantity__isnull=False, price_tier__isnull=False)
                ),
                name="hold_ga_fields_together",
            ),
        ]

    def __str__(self):
        return f"Hold #{self.pk} for {self.performance}"


class HoldSeat(TenantScopedModel):
    """Through row for a reserved-seat Hold: one row per held Seat, carrying
    the PriceTier that applied when it was selected."""

    hold = models.ForeignKey(Hold, on_delete=models.CASCADE, related_name="hold_seats")
    seat = models.ForeignKey(Seat, on_delete=models.CASCADE, related_name="hold_seats")
    price_tier = models.ForeignKey(PriceTier, on_delete=models.PROTECT, related_name="hold_seats")

    class Meta(TenantScopedModel.Meta):
        indexes = TenantScopedModel.Meta.indexes + [
            models.Index(fields=["hold"]),
            models.Index(fields=["seat"]),
        ]
        constraints = [
            models.UniqueConstraint(fields=["hold", "seat"], name="unique_seat_per_hold"),
        ]

    def __str__(self):
        return f"{self.seat} on hold #{self.hold_id}"


class Order(TenantScopedModel):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        PAID = "paid", "Paid"
        CANCELLED = "cancelled", "Cancelled"
        REFUNDED = "refunded", "Refunded"

    performance = models.ForeignKey(Performance, on_delete=models.PROTECT, related_name="orders")
    buyer_email = models.EmailField()
    buyer_name = models.CharField(max_length=255, blank=True)
    total = models.DecimalField(max_digits=10, decimal_places=2)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)

    # Public-facing lookup token for the confirmation page (/tickets/<token>/).
    token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)

    # Phase 4 (Stripe checkout + webhooks): fields only, no logic here.
    stripe_checkout_session_id = models.CharField(max_length=255, blank=True, null=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta(TenantScopedModel.Meta):
        indexes = TenantScopedModel.Meta.indexes + [
            models.Index(fields=["organization", "performance"]),
            models.Index(fields=["organization", "status"]),
            models.Index(fields=["stripe_checkout_session_id"]),
        ]
        ordering = ["-created_at"]

    def __str__(self):
        return f"Order {self.token} ({self.status})"


class OrderItem(TenantScopedModel):
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="items")
    price_tier = models.ForeignKey(PriceTier, on_delete=models.PROTECT, related_name="order_items")
    seat = models.ForeignKey(
        Seat, on_delete=models.PROTECT, null=True, blank=True, related_name="order_items"
    )
    quantity = models.PositiveIntegerField(default=1)
    unit_amount = models.DecimalField(max_digits=10, decimal_places=2)

    class Meta(TenantScopedModel.Meta):
        indexes = TenantScopedModel.Meta.indexes + [models.Index(fields=["order"])]

    def __str__(self):
        return f"{self.quantity} x {self.price_tier} on order {self.order_id}"


class Ticket(TenantScopedModel):
    class Status(models.TextChoices):
        VALID = "valid", "Valid"
        USED = "used", "Used"
        VOID = "void", "Void"

    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="tickets")
    performance = models.ForeignKey(Performance, on_delete=models.CASCADE, related_name="tickets")
    seat = models.ForeignKey(
        Seat, on_delete=models.SET_NULL, null=True, blank=True, related_name="tickets"
    )
    holder_name = models.CharField(max_length=255, blank=True)
    token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.VALID)
    used_at = models.DateTimeField(null=True, blank=True)
    scanned_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="scanned_tickets",
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta(TenantScopedModel.Meta):
        indexes = TenantScopedModel.Meta.indexes + [
            models.Index(fields=["organization", "performance"]),
            models.Index(fields=["performance", "seat"]),
        ]
        constraints = [
            # A seat can back at most one live (valid/used) ticket per
            # performance at a time; void tickets don't count so a
            # cancel-and-reissue doesn't collide with itself.
            models.UniqueConstraint(
                fields=["performance", "seat"],
                condition=models.Q(seat__isnull=False) & ~models.Q(status="void"),
                name="unique_live_ticket_per_performance_seat",
            ),
        ]

    def __str__(self):
        return f"Ticket {self.token} ({self.status})"


class Payment(TenantScopedModel):
    """Stub for Phase 4: fields only, no processing logic."""

    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="payments")
    provider = models.CharField(max_length=32, default="stripe")
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    status = models.CharField(max_length=32, blank=True)
    provider_ref = models.CharField(max_length=255, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta(TenantScopedModel.Meta):
        indexes = TenantScopedModel.Meta.indexes + [models.Index(fields=["order"])]

    def __str__(self):
        return f"{self.provider} payment for order {self.order_id}"
