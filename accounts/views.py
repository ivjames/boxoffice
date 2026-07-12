from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.forms import SetPasswordForm
from django.contrib.auth.tokens import default_token_generator
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.encoding import force_str
from django.utils.http import url_has_allowed_host_and_scheme, urlsafe_base64_decode
from django.views.decorators.http import require_POST

from tenants.decorators import require_tenant

from . import throttle
from .forms import StaffLoginForm
from .models import Membership, User


def _default_landing(request):
    """Where a just-logged-in staffer lands with no explicit ?next=. Scanners
    work the door only -- they have no overview/reports -- so send a
    scanner-only membership straight to the scan screen; everyone box office
    and up starts on the dashboard overview. Mirrors the nav/overview gating
    in dashboard.views + templates/dashboard/_nav.html."""
    membership = Membership.objects.filter(
        user=request.user, organization=request.organization
    ).first()
    if membership is not None and not membership.can_sell_tickets():
        return reverse("scan_home")
    return reverse("dashboard_overview")


def _safe_next(request, next_url):
    if next_url and url_has_allowed_host_and_scheme(
        next_url, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return next_url
    return _default_landing(request)


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
        if throttle.is_locked_out("staff-login", request):
            # Refuse before touching authenticate(), so a locked-out IP can't
            # keep probing. Same generic wording as a bad password.
            error = "Too many sign-in attempts. Please wait a few minutes and try again."
        elif form.is_valid():
            user = authenticate(
                request,
                username=form.cleaned_data["email"],
                password=form.cleaned_data["password"],
            )
            if user is None:
                throttle.register_failure("staff-login", request)
                error = "Incorrect email or password."
            elif not Membership.objects.filter(user=user, organization=request.organization).exists():
                # A real password but wrong theater is still a failed attempt
                # here -- count it so this can't be used to probe unthrottled.
                throttle.register_failure("staff-login", request)
                error = "This account doesn't have access to this theater."
            else:
                throttle.clear("staff-login", request)
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


def _user_from_uid(uidb64):
    try:
        uid = force_str(urlsafe_base64_decode(uidb64))
        return User.objects.get(pk=uid)
    except (TypeError, ValueError, OverflowError, User.DoesNotExist):
        return None


def set_password_view(request, uidb64, token):
    """Set-password landing for an invited teammate (accounts/invites.py).
    Token-gated with Django's default_token_generator -- the same signed
    uid+token flow password reset uses -- so it needs no login and isn't
    tenant-scoped (the invite link carries the tenant subdomain already).
    Using the link consumes it: once a password is set, the token no longer
    validates (it's bound to the user's has_usable_password state)."""
    user = _user_from_uid(uidb64)
    valid = user is not None and default_token_generator.check_token(user, token)

    if not valid:
        return render(request, "accounts/set_password.html", {"validlink": False})

    if request.method == "POST":
        form = SetPasswordForm(user, request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, "Password set. You can sign in now.")
            return redirect("login")
    else:
        form = SetPasswordForm(user)

    return render(
        request,
        "accounts/set_password.html",
        {"validlink": True, "form": form, "email": user.email},
    )
