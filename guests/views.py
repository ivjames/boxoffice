"""Guest self-service portal: a returning ticket buyer signs in with a magic
link (no password) and sees every order they've placed at this theater, each
linking through to the existing per-order tickets page (view / print / PDF).

All views are @require_tenant: the portal is a storefront concept and only
makes sense on a tenant subdomain, exactly like the cart/checkout views.
"""

import logging

from django.contrib import messages
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from accounts import throttle
from orders.models import Order
from tenants.decorators import require_tenant

from . import services
from .forms import GuestEmailForm
from .models import GuestAccount, normalize_email
from .tokens import read_login_token

logger = logging.getLogger(__name__)


@require_tenant
def guest_portal(request):
    """The buyer's "My tickets" home. Signed in -> list their orders. Not
    signed in -> the request-a-link form (guest_request_link handles its
    POST)."""
    guest = services.get_current_guest(request)
    if guest is None:
        return render(request, "guests/portal_signin.html", {"form": GuestEmailForm()})

    orders = (
        Order.objects.for_organization(request.organization)
        .filter(guest=guest)
        .select_related("performance", "performance__event", "performance__venue")
        .prefetch_related("tickets")
        .order_by("-created_at")
    )
    order_rows = [
        {"order": order, "ticket_count": order.tickets.count()} for order in orders
    ]
    return render(
        request,
        "guests/portal.html",
        {"guest": guest, "order_rows": order_rows},
    )


@require_tenant
@require_POST
def guest_request_link(request):
    """Email a magic sign-in link to the address entered on the portal.

    Anti-enumeration: the response is the SAME generic "check your email"
    message whether or not a GuestAccount exists for that address, so this
    endpoint can't be used to discover which emails have bought tickets here.
    A link is only actually sent when an account exists (accounts are created
    at purchase, so "has an account" == "has tickets to come back to")."""
    form = GuestEmailForm(request.POST)
    if not form.is_valid():
        return render(request, "guests/portal_signin.html", {"form": form})

    # Rate-limit link requests per IP: each valid request sends an email, so
    # an unthrottled endpoint is an email-bomb / enumeration lever. The
    # generic confirmation below is shown regardless, so a locked-out attacker
    # learns nothing new; count every request (not just failures) toward the
    # cap since every one triggers a send.
    if throttle.is_locked_out("guest-link", request):
        messages.success(
            request,
            "If that email has tickets with us, we've sent it a sign-in link. "
            "Check your inbox.",
        )
        return redirect("guest_portal")
    throttle.register_failure("guest-link", request)

    email = normalize_email(form.cleaned_data["email"])
    guest = GuestAccount.objects.for_organization(request.organization).filter(email=email).first()

    if not services.email_delivery_configured():
        # SMTP isn't set up yet, so an emailed link would silently go nowhere
        # and lock the buyer out of their tickets. Show the link on screen
        # instead. This necessarily reveals whether an account exists (the
        # anti-enumeration guarantee below only holds once email works), which
        # is an accepted trade-off for this bootstrap state -- it self-heals
        # the moment EMAIL_HOST is configured.
        return render(
            request,
            "guests/portal_signin.html",
            {
                "form": GuestEmailForm(),
                "login_link": services.build_login_link(guest, request) if guest else None,
                "requested_email": email,
            },
        )

    if guest is not None:
        try:
            services.send_login_link(guest, request)
        except Exception:
            # Don't leak transport state to the visitor (and don't reveal the
            # account exists by erroring differently) -- log for the operator
            # and still show the generic confirmation.
            logger.exception("Could not send guest sign-in link to guest %s.", guest.pk)

    messages.success(
        request,
        "If that email has tickets with us, we've sent it a sign-in link. "
        "Check your inbox.",
    )
    return redirect("guest_portal")


@require_tenant
def guest_verify(request):
    """Consume a magic-link token (?token=...): sign the guest in and bounce
    to the portal. An invalid/expired/wrong-tenant token just flashes an
    error and shows the request-a-link form again."""
    token = request.GET.get("token", "")
    guest_id = read_login_token(token, request.organization)
    guest = None
    if guest_id is not None:
        guest = (
            GuestAccount.objects.for_organization(request.organization)
            .filter(pk=guest_id)
            .first()
        )

    if guest is None:
        messages.error(
            request,
            "That sign-in link is invalid or has expired. Enter your email to get a new one.",
        )
        return redirect("guest_portal")

    services.login_guest(request, guest)
    messages.success(request, "You're signed in. Here are your tickets.")
    return redirect("guest_portal")


@require_tenant
@require_POST
def guest_logout(request):
    services.logout_guest(request)
    messages.info(request, "Signed out.")
    return redirect("guest_portal")
