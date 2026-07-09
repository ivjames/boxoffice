from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from tenants.decorators import require_tenant

from .forms import StaffLoginForm
from .models import Membership


def _safe_next(request, next_url):
    if next_url and url_has_allowed_host_and_scheme(
        next_url, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return next_url
    return reverse("dashboard_overview")


@require_tenant
def login_view(request):
    """Staff login, scoped to the tenant subdomain it's hit on. Authenticates
    the User globally (accounts.User has no notion of tenant), then --
    crucially -- only calls django.contrib.auth.login() (i.e. only starts a
    session) if the authenticated user also has a Membership for THIS
    request.organization. Correct credentials for a real user who simply
    works at a different theater are rejected here with the same generic
    message as a wrong password, so this endpoint can't be used to probe
    which theaters an email address has access to.

    Belt-and-suspenders: even if a session somehow existed for the wrong
    org (e.g. a shared-cookie edge case), accounts.permissions re-checks
    Membership against request.organization on every dashboard/scan request
    -- login-time gating alone is not the isolation boundary.
    """
    if request.user.is_authenticated and Membership.objects.filter(
        user=request.user, organization=request.organization
    ).exists():
        return redirect(_safe_next(request, request.GET.get("next")))

    error = None
    if request.method == "POST":
        form = StaffLoginForm(request.POST)
        next_url = request.POST.get("next", "")
        if form.is_valid():
            user = authenticate(
                request,
                username=form.cleaned_data["email"],
                password=form.cleaned_data["password"],
            )
            if user is None:
                error = "Incorrect email or password."
            elif not Membership.objects.filter(user=user, organization=request.organization).exists():
                error = "This account doesn't have access to this theater."
            else:
                login(request, user)
                messages.success(request, f"Welcome back, {user.get_short_name()}.")
                return redirect(_safe_next(request, next_url))
    else:
        form = StaffLoginForm()
        next_url = request.GET.get("next", "")

    return render(
        request,
        "accounts/login.html",
        {"form": form, "error": error, "next": next_url},
    )


@require_tenant
@require_POST
def logout_view(request):
    logout(request)
    messages.info(request, "Signed out.")
    return redirect("login")
