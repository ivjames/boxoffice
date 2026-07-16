from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.views.decorators.http import require_POST
from django.views.generic import CreateView, ListView, UpdateView

from accounts.permissions import ManagerRequiredMixin, manager_required
from promotions.models import PromoCode

from ..forms import PromoCodeForm


# --- promo codes (manager+) -------------------------------------------------
#
# v1 is org-wide only (no per-event scoping yet -- see promotions.models.
# PromoCode's docstring), so this is a flat list/create/edit CRUD, same shape
# as EventListView/EventCreateView/EventUpdateView above. Codes are never
# hard-deleted (is_active doubles as the archive flag): promo_deactivate is
# the one mutation endpoint, toggling that flag in either direction.


class PromoCodeListView(ManagerRequiredMixin, ListView):
    template_name = "dashboard/promo_list.html"
    context_object_name = "promos"

    def get_queryset(self):
        return PromoCode.objects.filter(organization=self.request.organization).order_by(
            "-created_at"
        )


class PromoCodeCreateView(ManagerRequiredMixin, CreateView):
    model = PromoCode
    form_class = PromoCodeForm
    template_name = "dashboard/promo_form.html"

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["organization"] = self.request.organization
        return kwargs

    def form_valid(self, form):
        form.instance.organization = self.request.organization
        messages.success(self.request, f"Created promo code {form.instance.code}.")
        return super().form_valid(form)

    def get_success_url(self):
        return reverse("dashboard_promo_list")


class PromoCodeUpdateView(ManagerRequiredMixin, UpdateView):
    form_class = PromoCodeForm
    template_name = "dashboard/promo_form.html"

    def get_queryset(self):
        return PromoCode.objects.filter(organization=self.request.organization)

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["organization"] = self.request.organization
        return kwargs

    def form_valid(self, form):
        messages.success(self.request, f"Updated promo code {form.instance.code}.")
        return super().form_valid(form)

    def get_success_url(self):
        return reverse("dashboard_promo_list")


@manager_required
@require_POST
def promo_deactivate(request, pk):
    """Toggle a promo code's is_active flag -- doubles as BOTH "deactivate"
    and "reactivate" (the button label flips based on current state; see
    promo_list.html). Codes are never hard-deleted (PromoCode's docstring),
    so this is the only way to retire/restore one. Org-scoped like every
    other dashboard mutation: a pk for another org's code 404s."""
    promo = get_object_or_404(PromoCode, pk=pk, organization=request.organization)
    promo.is_active = not promo.is_active
    promo.save(update_fields=["is_active"])
    if promo.is_active:
        messages.success(request, f"Reactivated {promo.code}.")
    else:
        messages.success(request, f"Deactivated {promo.code}.")
    return redirect("dashboard_promo_list")
