from decimal import Decimal

from django.contrib import messages
from django.db.models import Sum
from django.shortcuts import redirect, render

from accounts.permissions import manager_required
from donations.services import get_or_create_general_fund
from orders.models import Order, OrderItem

from ._common import csv_response

from ..forms import DonationSettingsForm


# --- donations (manager+) --------------------------------------------------
#
# v1 is a single org-wide campaign (DonationCampaign's docstring), so there's
# one settings form (not a list/create/edit CRUD like promo codes) plus a
# report over paid donation OrderItems -- mirrors the promo section's shape
# where it applies, and the existing dashboard CSV-export convention where
# an endpoint takes `?format=csv` rather than a dedicated URL (see
# donations_report below).


@manager_required
def donation_settings(request):
    """Settings form for the org's single donation campaign -- on/off switch,
    quick-pick preset amounts, and the nonprofit acknowledgment blurb. Loads
    (creating on first visit) via get_or_create_general_fund, same as every
    other donation entry point resolves "the org's campaign"."""
    campaign = get_or_create_general_fund(request.organization)
    if request.method == "POST":
        form = DonationSettingsForm(request.POST, instance=campaign)
        if form.is_valid():
            form.save()
            messages.success(request, "Donation settings saved.")
            return redirect("dashboard_donation_settings")
    else:
        form = DonationSettingsForm(instance=campaign)
    return render(request, "dashboard/donation_settings.html", {"form": form, "campaign": campaign})


@manager_required
def donations_report(request):
    """Totals + a row per paid donation, org-scoped, with optional
    ?start=YYYY-MM-DD&end=YYYY-MM-DD filters on Order.created_at.
    `?format=csv` streams the same rows as a download instead of rendering
    the HTML table -- mirrors the query-param CSV-export convention used
    elsewhere in the dashboard rather than adding a second URL."""
    organization = request.organization
    items = (
        OrderItem.objects.filter(
            organization=organization, kind=OrderItem.Kind.DONATION, order__status=Order.Status.PAID
        )
        .select_related("order", "order__guest", "donation_campaign")
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
        return csv_response(
            "donations.csv",
            ["Date", "Order token", "Buyer email", "Buyer name", "Campaign", "Amount"],
            (
                [
                    item.order.created_at.strftime("%Y-%m-%d %H:%M"),
                    item.order.token,
                    item.order.buyer_email,
                    item.order.buyer_name,
                    item.donation_campaign.name if item.donation_campaign_id else "",
                    item.unit_amount,
                ]
                for item in items
            ),
        )

    return render(
        request,
        "dashboard/donation_report.html",
        {"items": items, "total": total, "start": start, "end": end},
    )
