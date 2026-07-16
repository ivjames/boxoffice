from decimal import Decimal

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render

from accounts.permissions import manager_required
from campaigns.services import audience_queryset
from guests.models import GuestAccount
from orders.models import Order
from passes.models import PassPurchase

from ._common import csv_response

from ..forms import GuestTagsNotesForm


# --- audience / CRM (manager+, Phase 4) -------------------------------------
#
# The guest list + per-guest detail. Mirrors the donations/passes report
# shape (search/filter GET params, `?format=csv` export) rather than a plain
# ListView, since audience_queryset (campaigns.services) already does all the
# filtering/annotation work -- this view is a thin GET-param-to-kwargs
# translation over it, same division of labor as donations_report/pass_report
# over their own OrderItem querysets.


@manager_required
def audience_list(request):
    organization = request.organization
    search = request.GET.get("search", "").strip()
    tag = request.GET.get("tag", "").strip()
    # opt_in is a tri-state GET param ("" = everyone, "1" = opted in, "0" =
    # opted out) -- translated to audience_queryset's True/False/None kwarg.
    opt_in_param = request.GET.get("opt_in", "").strip()
    opt_in = {"1": True, "0": False}.get(opt_in_param)

    guests = audience_queryset(organization, search=search, opt_in=opt_in, tag=tag)

    if request.GET.get("format") == "csv":
        return csv_response(
            "audience.csv",
            ["Email", "Name", "Opted in", "Orders", "Lifetime value", "Tags"],
            (
                [
                    guest.email,
                    guest.name,
                    "yes" if guest.marketing_opt_in else "no",
                    guest.order_count,
                    guest.ltv or Decimal("0.00"),
                    guest.tags,
                ]
                for guest in guests
            ),
        )

    return render(
        request,
        "dashboard/audience_list.html",
        {"guests": guests, "search": search, "tag": tag, "opt_in": opt_in_param},
    )


@manager_required
def audience_detail(request, pk):
    """One guest's CRM record: order/pass history (read-only) plus the
    editable tags/notes form. Consent itself is never editable here -- see
    GuestTagsNotesForm's docstring -- only the guest's own portal toggle or
    the unsubscribe link can change marketing_opt_in."""
    organization = request.organization
    guest = get_object_or_404(GuestAccount.objects.for_organization(organization), pk=pk)

    if request.method == "POST":
        form = GuestTagsNotesForm(request.POST, instance=guest)
        if form.is_valid():
            form.save()
            messages.success(request, "Saved.")
            return redirect("dashboard_audience_detail", pk=guest.pk)
    else:
        form = GuestTagsNotesForm(instance=guest)

    # Same order-history query shape as guests.views.guest_portal's own "My
    # tickets" list -- this is the staff-facing mirror of that self-service
    # view, over the same rows.
    orders = (
        Order.objects.for_organization(organization)
        .filter(guest=guest)
        .select_related("performance", "performance__event", "performance__venue")
        .prefetch_related("tickets")
        .order_by("-created_at")
    )
    order_rows = [{"order": order, "ticket_count": order.tickets.count()} for order in orders]

    pass_rows = list(
        PassPurchase.objects.filter(organization=organization, guest=guest)
        .select_related("product")
        .order_by("-created_at")
    )

    return render(
        request,
        "dashboard/audience_detail.html",
        {"guest": guest, "form": form, "order_rows": order_rows, "pass_rows": pass_rows},
    )
