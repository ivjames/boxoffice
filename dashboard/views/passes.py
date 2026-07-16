import csv
from decimal import Decimal

from django.contrib import messages
from django.db.models import Sum
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST
from django.views.generic import CreateView, ListView, UpdateView

from accounts.permissions import ManagerRequiredMixin, manager_required
from orders.models import Order, OrderItem
from passes.models import PassProduct, PassPurchase
from passes.services import remaining_admissions

from ..forms import PassProductForm


# --- passes (manager+) ------------------------------------------------------
#
# Mirrors the promo-code CRUD shape above: flat list/create/edit, is_active
# doubles as the archive/enable flag (a PassProduct is never hard-deleted --
# its `purchases` PROTECT the row, same stance as PromoCode), pass_toggle is
# the one flip-both-ways mutation endpoint. See passes.models.PassProduct's
# docstring.


class PassProductListView(ManagerRequiredMixin, ListView):
    template_name = "dashboard/pass_list.html"
    context_object_name = "products"

    def get_queryset(self):
        return PassProduct.objects.filter(organization=self.request.organization).order_by(
            "-created_at"
        )


class PassProductCreateView(ManagerRequiredMixin, CreateView):
    model = PassProduct
    form_class = PassProductForm
    template_name = "dashboard/pass_form.html"

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["organization"] = self.request.organization
        return kwargs

    def form_valid(self, form):
        form.instance.organization = self.request.organization
        messages.success(self.request, f"Created “{form.instance.name}”.")
        return super().form_valid(form)

    def get_success_url(self):
        return reverse("dashboard_pass_list")


class PassProductUpdateView(ManagerRequiredMixin, UpdateView):
    form_class = PassProductForm
    template_name = "dashboard/pass_form.html"

    def get_queryset(self):
        return PassProduct.objects.filter(organization=self.request.organization)

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["organization"] = self.request.organization
        return kwargs

    def form_valid(self, form):
        messages.success(self.request, f"Updated “{form.instance.name}”.")
        return super().form_valid(form)

    def get_success_url(self):
        return reverse("dashboard_pass_list")


@manager_required
@require_POST
def pass_toggle(request, pk):
    """Toggle a pass product's is_active flag -- doubles as BOTH "deactivate"
    (hide from the storefront, block new sales) and "reactivate". Mirrors
    promo_deactivate exactly; past PassPurchases are untouched either way
    (their entitlement terms were already snapshotted at purchase -- see
    PassPurchase's docstring)."""
    product = get_object_or_404(PassProduct, pk=pk, organization=request.organization)
    product.is_active = not product.is_active
    product.save(update_fields=["is_active"])
    if product.is_active:
        messages.success(request, f"Reactivated {product.name}.")
    else:
        messages.success(request, f"Deactivated {product.name}.")
    return redirect("dashboard_pass_list")


@manager_required
def pass_report(request):
    """Sold passes (paid PASS OrderItems), date-filterable + CSV export --
    same shape as donations_report above -- plus OUTSTANDING LIABILITY: how
    many admissions the theater still owes against live (ACTIVE) purchases,
    and roughly what they're worth. A REFUNDED purchase, or a flex purchase
    that's run dry (EXHAUSTED), owes nothing more, so only ACTIVE purchases
    count.

    flex_value_outstanding is computed in PYTHON per purchase (fine at v1
    scale, per the roadmap note) as credits_remaining * (price paid /
    credit_count). "Price paid" is purchase.order.total: fulfill_pass_purchase
    always creates exactly one Order with total=product.price for a pass sale
    (no promo/donation can attach to it), so the order total IS the price paid
    for that pass -- no second query into its OrderItems needed."""
    organization = request.organization
    items = (
        OrderItem.objects.filter(
            organization=organization, kind=OrderItem.Kind.PASS, order__status=Order.Status.PAID
        )
        .select_related("order", "order__guest", "pass_product")
        .order_by("-order__created_at")
    )

    start = request.GET.get("start", "").strip()
    end = request.GET.get("end", "").strip()
    if start:
        items = items.filter(order__created_at__date__gte=start)
    if end:
        items = items.filter(order__created_at__date__lte=end)

    total = items.aggregate(total=Sum("unit_amount"))["total"] or Decimal("0.00")

    if request.GET.get("format") == "csv":
        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = 'attachment; filename="passes.csv"'
        writer = csv.writer(response)
        writer.writerow(["Date", "Order token", "Buyer email", "Product", "Amount"])
        for item in items:
            writer.writerow(
                [
                    item.order.created_at.strftime("%Y-%m-%d %H:%M"),
                    item.order.token,
                    item.order.buyer_email,
                    item.pass_product.name if item.pass_product_id else "",
                    item.unit_amount,
                ]
            )
        return response

    active_purchases = PassPurchase.objects.filter(
        organization=organization, status=PassPurchase.Status.ACTIVE
    ).select_related("order")

    flex_credits_outstanding = 0
    flex_value_outstanding = Decimal("0.00")
    season_admissions_outstanding = 0
    # All-events (empty covered_events) season passes are unbounded -- see
    # passes.services.remaining_admissions's docstring -- so they're counted
    # separately rather than folded into the numeric admissions total.
    unbounded_season_count = 0
    for purchase in active_purchases:
        if purchase.kind == PassProduct.Kind.FLEX:
            remaining = purchase.credits_remaining or 0
            flex_credits_outstanding += remaining
            if purchase.credit_count and remaining:
                price_paid = purchase.order.total if purchase.order_id else Decimal("0.00")
                flex_value_outstanding += (price_paid / purchase.credit_count) * remaining
        else:
            remaining = remaining_admissions(purchase)
            if remaining is None:
                unbounded_season_count += 1
            else:
                season_admissions_outstanding += remaining

    flex_value_outstanding = flex_value_outstanding.quantize(Decimal("0.01"))

    return render(
        request,
        "dashboard/pass_report.html",
        {
            "items": items,
            "total": total,
            "start": start,
            "end": end,
            "flex_credits_outstanding": flex_credits_outstanding,
            "flex_value_outstanding": flex_value_outstanding,
            "season_admissions_outstanding": season_admissions_outstanding,
            "unbounded_season_count": unbounded_season_count,
        },
    )
