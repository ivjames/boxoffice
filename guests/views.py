"""Guest self-service portal: a returning ticket buyer signs in with a magic
link (no password) and sees every order they've placed at this theater, each
linking through to the existing per-order tickets page (view / print / PDF).

All views are @require_tenant: the portal is a storefront concept and only
makes sense on a tenant subdomain, exactly like the cart/checkout views.
"""

import logging

from django.contrib import messages
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from accounts import throttle
from orders.models import Order
from passes import services as pass_services
from passes.models import PassPurchase
from tenants.decorators import require_tenant

from . import services
from .forms import GuestEmailForm
from .models import GuestAccount, normalize_email
from .tokens import read_login_token, read_unsubscribe_token

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

    # Phase 3: "My passes" -- every pass this guest owns (any status), each
    # annotated with its live remaining-admissions figure and whether it can
    # be redeemed right now (see passes.services). Read-only lookups; nothing
    # here mutates a pass or its entitlement -- redeeming one is the separate
    # pass_redeem_start POST (passes/views.py).
    passes_qs = (
        PassPurchase.objects.filter(organization=request.organization, guest=guest)
        .select_related("product")
        .order_by("-created_at")
    )
    pass_rows = [
        {
            "pass_purchase": pass_purchase,
            "remaining": pass_services.remaining_admissions(pass_purchase),
            "redeemable": pass_services.redeemable_now(pass_purchase),
        }
        for pass_purchase in passes_qs
    ]

    return render(
        request,
        "guests/portal.html",
        {"guest": guest, "order_rows": order_rows, "pass_rows": pass_rows},
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


# --- marketing preferences (Phase 4 CRM) -----------------------------------


@require_tenant
@require_POST
def guest_preferences(request):
    """The portal's email-preferences toggle -- a signed-in guest flipping
    their own marketing consent on/off. Requires sign-in (unlike
    guest_unsubscribe below, which deliberately doesn't): this is reached
    from inside the portal itself, not a bare emailed link, so there's
    already a guest in the session to act on. Uses set_marketing_opt_in (the
    two-way setter), not record_marketing_opt_in (checkout's one-way-only
    setter) -- from the portal a guest can turn consent OFF just as easily as
    on."""
    guest = services.get_current_guest(request)
    if guest is None:
        messages.error(request, "Sign in to your account to update your email preferences.")
        return redirect("guest_portal")

    opted_in = request.POST.get("marketing_opt_in") in ("on", "1", "true")
    services.set_marketing_opt_in(guest, opted_in)
    if opted_in:
        messages.success(request, "You're subscribed to email updates.")
    else:
        messages.success(request, "You've been unsubscribed from email updates.")
    return redirect("guest_portal")


@require_tenant
@csrf_exempt
def guest_unsubscribe(request):
    """Unsubscribe from a campaign email's footer link -- deliberately NOT
    sign-in gated, since the whole point is that it works straight from the
    inbox with no portal session. The signed token is the capability; it's
    exactly as much proof of "this is that guest's inbox" as a login link.

    Three ways in, all token-authed, all handled here:
      - **GET ?token=...** -- the human clicks the footer link. Unsubscribes,
        then renders a confirm page with a Resubscribe button (in case it was
        a fat-finger).
      - **RFC 8058 one-click POST** -- the mail client's own "Unsubscribe"
        button (advertised via the List-Unsubscribe / List-Unsubscribe-Post
        headers we send) POSTs `List-Unsubscribe=One-Click` to the *exact*
        header URI, so the token rides the QUERY STRING, not the body. This
        MUST opt the guest out (a resubscribe here would defeat the native
        control) and just returns 200 -- mail clients don't render the body.
      - **confirm-page Resubscribe POST** -- the only path that opts back IN,
        disambiguated by an explicit `action=resubscribe` field (not "any
        POST", which would collide with the one-click POST above).

    This view is @csrf_exempt because the one-click POST originates from a mail
    provider and carries no CSRF cookie/token; the signed token is the auth,
    and the only state change a forged request could cause is opting the token's
    own guest in or out -- not a meaningful attack (and the guest can trivially
    flip it back). Token is read query-first so the one-click POST (token in the
    URL) and the confirm form (token in the body) both resolve.

    read_unsubscribe_token re-checks the token's embedded org id against
    request.organization (see guests.tokens), so a token minted for one theater
    can't be replayed on another. An invalid/expired/wrong-tenant token renders
    the friendly "invalid" branch, not a 404/500 -- a stale or mis-copied footer
    link is expected, not an error."""
    token = request.GET.get("token") or request.POST.get("token") or ""
    guest_id = read_unsubscribe_token(token, request.organization)
    guest = None
    if guest_id is not None:
        guest = (
            GuestAccount.objects.for_organization(request.organization)
            .filter(pk=guest_id)
            .first()
        )

    # RFC 8058 native one-click: the mail client POSTs List-Unsubscribe=One-Click.
    one_click = (
        request.method == "POST"
        and request.POST.get("List-Unsubscribe") == "One-Click"
    )
    # The confirm page's Resubscribe button is the ONLY opt-back-in path.
    resubscribe = request.method == "POST" and request.POST.get("action") == "resubscribe"

    if guest is not None:
        services.set_marketing_opt_in(guest, opted_in=resubscribe)

    if one_click:
        # Mail clients discard the response body; a bare 200 is the whole
        # contract. Do this before rendering so a missing/invalid guest here
        # (already unsubscribed, stale token) still 200s rather than 404s.
        return HttpResponse(status=200)

    if guest is None:
        return render(request, "guests/unsubscribe_confirm.html", {"invalid": True})

    return render(
        request,
        "guests/unsubscribe_confirm.html",
        {"guest": guest, "token": token, "resubscribed": resubscribe},
    )
