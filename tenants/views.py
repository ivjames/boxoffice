import logging

from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.templatetags.static import static as static_url
from django.urls import reverse
from django.utils import timezone

from accounts import throttle
from events.models import Event, Performance
from orders import services

from .emails import notify_contact_inquiry
from .forms import PlatformContactForm
from .models import ContactInquiry

logger = logging.getLogger(__name__)

# accounts.throttle scope for the landing contact form: caps how many
# inquiries one IP can submit per window (same knobs as the login throttle,
# LOGIN_RATELIMIT_* -- see config/settings/base.py).
_CONTACT_THROTTLE_SCOPE = "platform-contact"


def healthz(request):
    return JsonResponse({"status": "ok"})


def robots_txt(request):
    """Crawler policy, served on every host. Public storefront pages are
    crawlable; staff/transactional surfaces (dashboard, scanner + redeem,
    cart/checkout, the guest portal, admin, login) are disallowed -- they're
    private or per-session and have no business in an index. Points crawlers
    at the per-host sitemap."""
    lines = [
        "User-agent: *",
        "Disallow: /dashboard/",
        "Disallow: /scan/",
        "Disallow: /S/",
        "Disallow: /cart/",
        "Disallow: /checkout/",
        "Disallow: /account/",
        "Disallow: /admin/",
        "Disallow: /login/",
        f"Sitemap: {request.build_absolute_uri(reverse('sitemap_xml'))}",
    ]
    return HttpResponse("\n".join(lines) + "\n", content_type="text/plain")


def _published_upcoming_events(organization):
    """Published events for `organization` that have at least one published,
    still-upcoming performance -- the same visibility rule the storefront home
    uses, reused for the sitemap so it never lists a draft or past-only show."""
    now = timezone.now()
    events = (
        Event.objects.for_organization(organization)
        .filter(status=Event.Status.PUBLISHED)
        .prefetch_related("performances")
    )
    result = []
    for event in events:
        if any(
            p.status == p.Status.PUBLISHED and p.starts_at >= now
            for p in event.performances.all()
        ):
            result.append(event)
    return result


def sitemap_xml(request):
    """Per-host sitemap. On a tenant subdomain: the storefront home, the public
    FAQ, and each published upcoming event. On the platform host (no tenant):
    just the landing page -- and NEVER any tenant's catalog, mirroring home()'s
    isolation. Built directly (not via django.contrib.sitemaps) so host-based
    tenant scoping stays explicit."""
    urls = [request.build_absolute_uri("/")]
    if request.organization is not None:
        urls.append(request.build_absolute_uri(reverse("faq")))
        for event in _published_upcoming_events(request.organization):
            urls.append(request.build_absolute_uri(reverse("event_detail", args=[event.slug])))

    body = ['<?xml version="1.0" encoding="UTF-8"?>']
    body.append('<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">')
    for url in urls:
        body.append(f"  <url><loc>{url}</loc></url>")
    body.append("</urlset>")
    return HttpResponse("\n".join(body) + "\n", content_type="application/xml")


def favicon(request):
    """Redirect /favicon.ico (which browsers/crawlers request at the root
    regardless of markup) to the committed static SVG favicon."""
    return redirect(static_url("favicon.svg"), permanent=True)


def _card_pricing_and_availability(performance):
    """Presentation-only helper for the home event cards: the "from $X"
    price and a live availability count for a single (usually the soonest
    upcoming) Performance. Reuses orders.services' existing read-only
    availability/pricing helpers rather than re-deriving the rules here --
    booking logic itself is untouched."""
    if performance.seating_mode == Performance.SeatingMode.GA:
        tiers = list(performance.price_tiers.all())
        available = services.ga_available(performance)
    else:
        tiers = list(services.price_tiers_by_section(performance).values())
        available = services.reserved_available_count(performance)

    min_price = min((t.amount for t in tiers), default=None)
    return min_price, available


def contact(request):
    """POST target for the landing page's "Get in touch" form. Platform host
    only -- on a tenant subdomain this endpoint doesn't exist (404), the same
    isolation stance as home(): a theater's storefront never grows platform
    surfaces. GET just bounces to the form section.

    The inquiry is stored in the DB (ContactInquiry, triaged in /admin), so
    the form works before outbound mail is set up; notify_contact_inquiry
    layers a best-effort email on top once it is. Two abuse guards: the
    form's honeypot (bots get the success redirect but write nothing, so
    they can't tell they were caught) and the shared cache-backed IP
    throttle (accounts/throttle.py) capping stored inquiries per window.
    """
    if request.organization is not None:
        raise Http404
    if request.method != "POST":
        return redirect(reverse("home") + "#contact")

    form = PlatformContactForm(request.POST)
    if not form.is_valid():
        # Re-render the landing with the bound form so field errors show
        # inline in the #contact section, inputs preserved.
        return render(
            request,
            "tenants/platform_landing.html",
            {"contact_form": form, "contact_sent": False},
        )

    if form.is_spam():
        logger.info("Contact form honeypot tripped; dropping submission.")
        return redirect(reverse("home") + "?sent=1#contact")

    if throttle.is_locked_out(_CONTACT_THROTTLE_SCOPE, request):
        form.add_error(
            None,
            "Too many messages from your network just now — please wait a few "
            "minutes and try again.",
        )
        return render(
            request,
            "tenants/platform_landing.html",
            {"contact_form": form, "contact_sent": False},
        )

    inquiry = ContactInquiry.objects.create(
        name=form.cleaned_data["name"],
        email=form.cleaned_data["email"],
        venue=form.cleaned_data["venue"],
        message=form.cleaned_data["message"],
    )
    throttle.register_failure(_CONTACT_THROTTLE_SCOPE, request)
    notify_contact_inquiry(inquiry)
    return redirect(reverse("home") + "?sent=1#contact")


def home(request):
    """
    Root URL. Renders the tenant storefront home (published events with at
    least one upcoming, published performance) when request.organization is
    set — i.e. on a real tenant subdomain — otherwise the platform landing
    page (reserved subdomain / bare host) — and does NOT touch tenant data in
    that case, so the platform host never leaks a theater's catalog.
    """
    if request.organization is None:
        return render(
            request,
            "tenants/platform_landing.html",
            {
                "contact_form": PlatformContactForm(),
                # PRG landing: contact() redirects here with ?sent=1#contact,
                # which swaps the form for a thank-you card.
                "contact_sent": request.GET.get("sent") == "1",
            },
        )

    now = timezone.now()
    events = Event.objects.for_organization(request.organization).filter(
        status=Event.Status.PUBLISHED
    ).prefetch_related("performances")

    events_with_upcoming = []
    for event in events:
        upcoming = sorted(
            (
                p
                for p in event.performances.all()
                if p.status == p.Status.PUBLISHED and p.starts_at >= now
            ),
            key=lambda p: p.starts_at,
        )
        if upcoming:
            min_price, available = _card_pricing_and_availability(upcoming[0])
            events_with_upcoming.append(
                {
                    "event": event,
                    "performances": upcoming,
                    "min_price": min_price,
                    "available": available,
                }
            )

    events_with_upcoming.sort(key=lambda row: row["performances"][0].starts_at)

    return render(
        request,
        "tenants/storefront_home.html",
        {"events_with_upcoming": events_with_upcoming},
    )
