from django.contrib import messages
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect
from django.views.decorators.http import require_POST
from django.views.generic import DetailView, ListView

from accounts.permissions import BoxOfficeRequiredMixin, box_office_required
from orders.emails import send_order_receipt
from orders.models import Order
from orders.services import void_order
from passes.services import restore_redemptions_for_order
from payments.services import RefundError, refund_order


# --- orders (box_office+) -------------------------------------------------


class OrderListView(BoxOfficeRequiredMixin, ListView):
    template_name = "dashboard/order_list.html"
    context_object_name = "orders"
    paginate_by = 25

    def get_queryset(self):
        qs = Order.objects.filter(organization=self.request.organization).select_related(
            "performance", "performance__event"
        )
        query = self.request.GET.get("q", "").strip()
        if query:
            # token is a short opaque string now (orders.models.new_token), so
            # match it directly instead of parsing the query as a UUID.
            filters = (
                Q(buyer_email__icontains=query)
                | Q(buyer_name__icontains=query)
                | Q(token=query)
            )
            qs = qs.filter(filters)
        return qs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["q"] = self.request.GET.get("q", "")
        return context


class OrderDetailView(BoxOfficeRequiredMixin, DetailView):
    template_name = "dashboard/order_detail.html"
    context_object_name = "order"

    def get_queryset(self):
        return Order.objects.filter(organization=self.request.organization).select_related(
            "performance", "performance__event", "performance__venue"
        )

    def get_object(self, queryset=None):
        queryset = queryset if queryset is not None else self.get_queryset()
        return get_object_or_404(queryset, token=self.kwargs["token"])

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["tickets"] = self.object.tickets.select_related(
            "seat", "seat__section", "scanned_by"
        ).order_by("id")
        # Phase 2: a kind-aware line-item table -- a ticket item shows its
        # existing seat/tier text, a donation item shows the gift amount +
        # campaign, so a donation-only order's detail page has something to
        # show besides the (now-guarded) performance line and an empty
        # tickets table. select_related covers every FK a line item's kind
        # might read from, so the template never N+1s per row.
        context["items"] = self.object.items.select_related(
            "price_tier", "pricing_zone", "seat", "donation_campaign", "pass_product"
        ).order_by("id")
        # Phase 3: a REDEMPTION order (one that spent a pass on seats) carries
        # PassRedemption rows -- summarize them per pass so the detail page can
        # show "Redeemed with <product>: N ticket(s) (N credit(s))" without a
        # row-per-ticket table. Grouped in Python (not a template {% regroup %}
        # sum) since credits_used needs summing, not just counting.
        redemption_groups = {}
        for redemption in self.object.pass_redemptions.select_related(
            "pass_purchase__product"
        ):
            group = redemption_groups.setdefault(
                redemption.pass_purchase_id,
                {"product": redemption.pass_purchase.product, "count": 0, "credits": 0},
            )
            group["count"] += 1
            group["credits"] += redemption.credits_used
        context["pass_redemption_summary"] = list(redemption_groups.values())
        return context


# --- order actions (box_office+) ------------------------------------------
#
# The staff order surface used to be read-only; these three POST actions are
# what the built-in help center already tells box office they can do (resend
# tickets, cancel/void, refund -- see helpcenter/builtins.py). Each is
# org-scoped by token (a box-office user can't touch another tenant's order)
# and gated to box_office+.


def _org_order(request, token):
    return get_object_or_404(
        Order.objects.filter(organization=request.organization), token=token
    )


@box_office_required
@require_POST
def order_resend(request, token):
    """Re-send the confirmation email for an order (e.g. the buyer lost it or
    gave a typo'd address that's since been corrected) -- tickets, or (Phase
    2) a donation acknowledgment for a donation-only order, via the
    send_order_receipt dispatcher (orders.emails)."""
    order = _org_order(request, token)
    if not order.buyer_email:
        messages.error(request, "This order has no email address on file to send to.")
        return redirect("dashboard_order_detail", token=order.token)
    try:
        send_order_receipt(order)
    except Exception:  # delivery/transport failure -- don't 500 the dashboard
        messages.error(request, "Couldn't send the email just now. Please try again.")
    else:
        messages.success(request, f"Resent the receipt to {order.buyer_email}.")
    return redirect("dashboard_order_detail", token=order.token)


@box_office_required
@require_POST
def order_cancel(request, token):
    """Cancel an order: void its tickets and free the inventory (see
    orders.services.void_order) without moving any money. Use this for a comp/
    test order or when a refund is handled outside the system; use Refund when
    the buyer paid via Stripe and should get their money back."""
    order = _org_order(request, token)
    if order.status in (Order.Status.CANCELLED, Order.Status.REFUNDED):
        messages.info(request, "That order is already cancelled.")
        return redirect("dashboard_order_detail", token=order.token)
    voided = void_order(order)
    # Phase 3: a cancelled PASS-REDEMPTION order comped its tickets against a
    # PassPurchase's entitlement (season event slot / flex credits) -- voiding
    # the tickets alone doesn't give that entitlement back. restore_redemptions_
    # for_order deletes the order's PassRedemption rows (freeing a season event
    # slot) and restores any burned flex credits. A no-op (returns 0) for the
    # common case of an order that never redeemed a pass. dashboard may import
    # passes; orders may not (see passes.services' dependency-direction note),
    # which is why this lives here rather than in orders.services.void_order.
    restore_redemptions_for_order(order)
    order.status = Order.Status.CANCELLED
    order.save(update_fields=["status"])
    messages.success(
        request, f"Cancelled the order and released {voided} ticket(s) back to inventory."
    )
    return redirect("dashboard_order_detail", token=order.token)


@box_office_required
@require_POST
def order_refund(request, token):
    """Refund a paid order in full (Stripe Refund on the connected account for
    a real charge; a recorded reversal for a stub/test order), voiding its
    tickets and freeing inventory -- see payments.services.refund_order.
    Idempotent: refunding an order that isn't currently paid is a no-op."""
    order = _org_order(request, token)
    try:
        refunded = refund_order(order)
    except RefundError:
        messages.error(
            request,
            "Stripe couldn't process the refund. Check the order in the Stripe "
            "dashboard and try again.",
        )
        return redirect("dashboard_order_detail", token=order.token)
    if refunded:
        messages.success(request, "Refunded the order and voided its tickets.")
    else:
        messages.info(request, "That order isn't in a refundable state.")
    return redirect("dashboard_order_detail", token=order.token)
