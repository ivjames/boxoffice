from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest import mock
from zoneinfo import ZoneInfo

from django.contrib.admin.sites import AdminSite
from django.contrib.auth import get_user_model
from django.contrib.messages.storage.fallback import FallbackStorage
from django.core.management import CommandError, call_command
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from events.models import Event, GAAllocation, Performance, PriceTier
from tenants.admin import OrganizationAdmin, OrganizationAdminForm
from tenants.models import Organization
from venues.models import Seat, SeatingChart, Section, Venue
from venues.tests import make_org

_CMD = "tenants.management.commands.provision_pending_tenants"
In = Organization.InfraStatus


def _completed(returncode=0, stdout="", stderr=""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


class OrganizationAdminFormTests(TestCase):
    """The admin form gives Organization sensible widgets: write-only Stripe
    secrets, native color pickers, and validated dropdowns for timezone and
    currency. The change form also carries a per-object provision button."""

    def setUp(self):
        self.admin_user = get_user_model().objects.create_superuser(
            email="admin@boxo.show", password="pw"
        )
        self.client.force_login(self.admin_user)

    # --- write-only secret handling (form-level) ---

    def test_blank_secret_keeps_the_current_value(self):
        org = make_org("roxy", stripe_secret_key="sk_live_EXISTING")
        form = OrganizationAdminForm(instance=org)
        form.cleaned_data = {"stripe_secret_key": ""}
        # A blank submit must not wipe the live key.
        self.assertEqual(form.clean_stripe_secret_key(), "sk_live_EXISTING")

    def test_new_secret_overwrites(self):
        org = make_org("roxy", stripe_secret_key="sk_live_EXISTING")
        form = OrganizationAdminForm(instance=org)
        form.cleaned_data = {"stripe_secret_key": "sk_live_NEW"}
        self.assertEqual(form.clean_stripe_secret_key(), "sk_live_NEW")

    # --- rendered change form ---

    def _change_url(self, org):
        return reverse("admin:tenants_organization_change", args=[org.pk])

    def test_stored_secret_never_reaches_the_html(self):
        org = make_org("roxy", stripe_secret_key="sk_live_TOPSECRET")
        resp = self.client.get(self._change_url(org))
        self.assertNotContains(resp, "sk_live_TOPSECRET")
        # Rendered as a password input, not a plain text field.
        self.assertContains(resp, 'type="password"')

    def test_widgets_are_pickers_and_dropdowns(self):
        org = make_org("roxy")
        resp = self.client.get(self._change_url(org))
        self.assertContains(resp, 'type="color"')  # color pickers
        self.assertContains(resp, '<select name="timezone"')  # tz dropdown
        self.assertContains(resp, '<select name="currency"')  # currency dropdown

    def test_change_form_has_provision_button(self):
        org = make_org("roxy")
        resp = self.client.get(self._change_url(org))
        self.assertContains(resp, "Provision infrastructure")
        self.assertContains(
            resp, reverse("admin:tenants_organization_provision", args=[org.pk])
        )

    # --- per-object provision button ---

    def test_provision_button_queues_this_tenant(self):
        org = make_org("roxy")
        self.assertEqual(org.infra_status, In.NONE)
        url = reverse("admin:tenants_organization_provision", args=[org.pk])
        resp = self.client.post(url)
        self.assertRedirects(resp, self._change_url(org))
        org.refresh_from_db()
        self.assertEqual(org.infra_status, In.PENDING)

    def test_provision_endpoint_ignores_get(self):
        org = make_org("roxy")
        url = reverse("admin:tenants_organization_provision", args=[org.pk])
        self.client.get(url)
        org.refresh_from_db()
        self.assertEqual(org.infra_status, In.NONE)  # unchanged; GET is a no-op


class ProvisionInfraAdminActionTests(TestCase):
    """The admin action only flips infra_status to PENDING (a DB write); the
    cron worker does the privileged DNS/nginx/TLS work."""

    def _run_action(self, queryset):
        request = RequestFactory().post("/admin/tenants/organization/")
        request.session = {}
        request._messages = FallbackStorage(request)
        OrganizationAdmin(Organization, AdminSite()).provision_infrastructure(request, queryset)
        return [m.message for m in request._messages]

    def test_queues_selected_and_skips_already_queued(self):
        fresh = Organization.objects.create(
            name="Fresh", slug="fresh", subdomain="fresh", contact_email="a@b.c"
        )
        already = Organization.objects.create(
            name="Queued", slug="queued", subdomain="queued", contact_email="a@b.c",
            infra_status=In.PENDING,
        )
        msgs = " ".join(self._run_action(Organization.objects.all()))

        fresh.refresh_from_db()
        already.refresh_from_db()
        self.assertEqual(fresh.infra_status, In.PENDING)
        self.assertEqual(already.infra_status, In.PENDING)
        self.assertIn("Queued 1", msgs)
        self.assertIn("Skipped 1", msgs)  # not "Skipped 2" — count taken pre-update

    def test_requeues_failed_and_recovers_stranded_provisioning(self):
        """Queueable from any non-PENDING state: retry a FAILED tenant, and
        recover one left PROVISIONING by a crashed worker."""
        failed = Organization.objects.create(
            name="Retry", slug="retry", subdomain="retry", contact_email="a@b.c",
            infra_status=In.FAILED,
        )
        stranded = Organization.objects.create(
            name="Stranded", slug="stranded", subdomain="stranded", contact_email="a@b.c",
            infra_status=In.PROVISIONING,
        )
        self._run_action(Organization.objects.filter(pk__in=[failed.pk, stranded.pk]))
        failed.refresh_from_db()
        stranded.refresh_from_db()
        self.assertEqual(failed.infra_status, In.PENDING)
        self.assertEqual(stranded.infra_status, In.PENDING)


class ProvisionPendingTenantsCommandTests(TestCase):
    """The root cron worker: claims PENDING rows and shells out to
    `bin/boxoffice add-tenant <sub> --infra-only`, recording the outcome."""

    def setUp(self):
        self.org = Organization.objects.create(
            name="Roxy", slug="roxy", subdomain="roxy", contact_email="a@b.c",
            infra_status=In.PENDING,
        )

    def test_success_marks_provisioned_and_calls_add_tenant(self):
        with mock.patch(f"{_CMD}.os.geteuid", return_value=0), \
             mock.patch(f"{_CMD}.os.access", return_value=True), \
             mock.patch(f"{_CMD}.subprocess.run", return_value=_completed(0, "==> Onboarded roxy.boxo.show")) as run:
            call_command("provision_pending_tenants")

        self.org.refresh_from_db()
        self.assertEqual(self.org.infra_status, In.PROVISIONED)
        self.assertIn("Onboarded", self.org.infra_message)
        args = run.call_args.args[0]
        self.assertEqual(args[1:], ["add-tenant", "roxy", "--infra-only"])

    def test_nonzero_exit_marks_failed(self):
        with mock.patch(f"{_CMD}.os.geteuid", return_value=0), \
             mock.patch(f"{_CMD}.os.access", return_value=True), \
             mock.patch(f"{_CMD}.subprocess.run", return_value=_completed(1, "", "certbot: DNS not propagated")):
            call_command("provision_pending_tenants")

        self.org.refresh_from_db()
        self.assertEqual(self.org.infra_status, In.FAILED)
        self.assertIn("certbot", self.org.infra_message)

    def test_not_root_leaves_pending_and_never_shells_out(self):
        with mock.patch(f"{_CMD}.os.geteuid", return_value=1000), \
             mock.patch(f"{_CMD}.subprocess.run") as run:
            call_command("provision_pending_tenants")

        self.org.refresh_from_db()
        self.assertEqual(self.org.infra_status, In.PENDING)
        run.assert_not_called()

    @override_settings(RESERVED_SUBDOMAINS={"www", "admin", "roxy"})
    def test_reserved_subdomain_fails_without_shelling_out(self):
        with mock.patch(f"{_CMD}.os.geteuid", return_value=0), \
             mock.patch(f"{_CMD}.os.access", return_value=True), \
             mock.patch(f"{_CMD}.subprocess.run") as run:
            call_command("provision_pending_tenants")

        self.org.refresh_from_db()
        self.assertEqual(self.org.infra_status, In.FAILED)
        self.assertIn("reserved", self.org.infra_message)
        run.assert_not_called()

    def test_no_pending_is_a_noop(self):
        self.org.infra_status = In.PROVISIONED
        self.org.save(update_fields=["infra_status"])
        with mock.patch(f"{_CMD}.os.geteuid", return_value=0), \
             mock.patch(f"{_CMD}.subprocess.run") as run:
            call_command("provision_pending_tenants")
        run.assert_not_called()


class CreateDemoTenantCommandTests(TestCase):
    def _counts(self):
        return {
            "orgs": Organization.objects.count(),
            "venues": Venue.objects.count(),
            "charts": SeatingChart.objects.count(),
            "sections": Section.objects.count(),
            "seats": Seat.objects.count(),
            "events": Event.objects.count(),
            "performances": Performance.objects.count(),
            "price_tiers": PriceTier.objects.count(),
            "ga_allocations": GAAllocation.objects.count(),
        }

    def test_creates_expected_rows(self):
        call_command("create_demo_tenant")

        org = Organization.objects.get(subdomain="roxy")
        self.assertTrue(org.is_active)

        venue = Venue.objects.get(organization=org)
        chart = SeatingChart.objects.get(organization=org, venue=venue)
        self.assertEqual(Section.objects.filter(organization=org, chart=chart).count(), 2)
        self.assertEqual(Seat.objects.filter(organization=org).count(), 70)

        event = Event.objects.get(organization=org)
        self.assertEqual(event.status, Event.Status.PUBLISHED)

        performances = Performance.objects.filter(organization=org, event=event)
        self.assertEqual(performances.count(), 2)

        ga_perf = performances.get(seating_mode=Performance.SeatingMode.GA)
        self.assertTrue(GAAllocation.objects.filter(performance=ga_perf, capacity=100).exists())

        reserved_perf = performances.get(seating_mode=Performance.SeatingMode.RESERVED)
        # 3 section-scoped tiers: Orchestra default, Balcony default, and the
        # Orchestra evening-premium override on the reserved performance
        # (events/pricing.py) -- see test_reserved_performance_pricing_has_a_demo_override.
        self.assertEqual(
            PriceTier.objects.filter(organization=org, section__chart=chart).count(), 3
        )
        self.assertTrue(PriceTier.objects.filter(organization=org, performance=ga_perf).exists())
        self.assertIsNotNone(reserved_perf)

    def test_reserved_performance_pricing_has_a_demo_override(self):
        """create_demo_tenant seeds ONE example per-performance section
        override (events/pricing.py resolve_seat_tier) so the feature is
        demonstrable out of the box: Orchestra's evening premium on the
        reserved performance beats its section-wide default."""
        call_command("create_demo_tenant")
        org = Organization.objects.get(subdomain="roxy")
        venue = Venue.objects.get(organization=org)
        chart = SeatingChart.objects.get(organization=org, venue=venue)
        orchestra = Section.objects.get(organization=org, chart=chart, name="Orchestra")
        balcony = Section.objects.get(organization=org, chart=chart, name="Balcony")
        reserved_perf = Performance.objects.get(
            organization=org, seating_mode=Performance.SeatingMode.RESERVED
        )

        default_tier = PriceTier.objects.get(
            organization=org, section=orchestra, performance__isnull=True
        )
        self.assertEqual(default_tier.amount, 65)

        override_tier = PriceTier.objects.get(
            organization=org, section=orchestra, performance=reserved_perf
        )
        self.assertEqual(override_tier.amount, 85)

        from events.pricing import resolve_seat_tier

        self.assertEqual(resolve_seat_tier(reserved_perf, orchestra), override_tier)
        # Balcony has no override -- still resolves to its plain default.
        balcony_tier = PriceTier.objects.get(organization=org, section=balcony)
        self.assertEqual(resolve_seat_tier(reserved_perf, balcony), balcony_tier)

    def test_idempotent_on_rerun(self):
        call_command("create_demo_tenant")
        first = self._counts()

        call_command("create_demo_tenant")
        second = self._counts()

        self.assertEqual(first, second)

    def test_custom_subdomain(self):
        call_command("create_demo_tenant", "--subdomain=globe")
        self.assertTrue(Organization.objects.filter(subdomain="globe").exists())


class ProvisionTenantCommandTests(TestCase):
    """Covers the DB half of `bin/boxoffice add-tenant` (no-wildcard onboarding)."""

    def test_creates_organization(self):
        call_command("provision_tenant", "roxy", "--name", "The Roxy Theater")

        org = Organization.objects.get(subdomain="roxy")
        self.assertEqual(org.name, "The Roxy Theater")
        self.assertEqual(org.slug, "roxy")
        self.assertTrue(org.is_active)
        self.assertEqual(org.contact_email, "boxoffice@roxy.localhost")

    def test_default_name_derived_from_subdomain(self):
        call_command("provision_tenant", "the-globe")
        org = Organization.objects.get(subdomain="the-globe")
        self.assertEqual(org.name, "The Globe")

    def test_idempotent_on_rerun(self):
        call_command("provision_tenant", "roxy", "--name", "The Roxy Theater")
        first_count = Organization.objects.count()

        # Re-running with a different --name does not rename the existing org.
        call_command("provision_tenant", "roxy", "--name", "Something Else")

        self.assertEqual(Organization.objects.count(), first_count)
        self.assertEqual(Organization.objects.get(subdomain="roxy").name, "The Roxy Theater")

    def test_rejects_reserved_subdomain(self):
        with self.assertRaises(CommandError):
            call_command("provision_tenant", "www")
        self.assertFalse(Organization.objects.filter(subdomain="www").exists())

    def test_rejects_invalid_subdomain(self):
        with self.assertRaises(CommandError):
            call_command("provision_tenant", "Not_Valid!")

    def test_custom_contact_email(self):
        call_command("provision_tenant", "roxy", "--contact-email", "box@roxy.example")
        self.assertEqual(
            Organization.objects.get(subdomain="roxy").contact_email, "box@roxy.example"
        )


class RemoveTenantCommandTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(
            name="The Roxy Theater",
            slug="roxy",
            subdomain="roxy",
            contact_email="box@roxy.example",
        )

    def test_default_deactivates_without_deleting(self):
        call_command("remove_tenant", "roxy")

        self.org.refresh_from_db()
        self.assertFalse(self.org.is_active)
        self.assertTrue(Organization.objects.filter(subdomain="roxy").exists())

    def test_deactivate_is_idempotent(self):
        call_command("remove_tenant", "roxy")
        call_command("remove_tenant", "roxy")

        self.org.refresh_from_db()
        self.assertFalse(self.org.is_active)

    def test_purge_deletes_the_organization(self):
        call_command("remove_tenant", "roxy", "--purge")
        self.assertFalse(Organization.objects.filter(subdomain="roxy").exists())

    def test_unknown_subdomain_raises(self):
        with self.assertRaises(CommandError):
            call_command("remove_tenant", "does-not-exist")



class PlatformHostTests(TestCase):
    """The platform host (a reserved subdomain / bare BASE_DOMAIN / an
    unrecognized host) is never a tenant: it serves the landing page and
    never leaks a theater's catalog. Real tenant subdomains resolve to their
    own Organization independently. pytest-django runs with DEBUG forced
    False, so a bare `self.client.get("/")` (Host: testserver) never matches
    BASE_DOMAIN ("localhost" in dev settings) and always takes the
    platform-host path in TenantMiddleware._resolve -- exactly like hitting
    the bare boxo.show host in prod with no subdomain.
    """

    def setUp(self):
        self.org = make_org("roxy")
        venue = Venue.objects.create(organization=self.org, name="Main Stage")
        event = Event.objects.create(
            organization=self.org,
            title="Roxy Season Opener",
            slug="roxy-season-opener",
            status=Event.Status.PUBLISHED,
        )
        performance = Performance.objects.create(
            organization=self.org,
            event=event,
            venue=venue,
            starts_at=timezone.now() + timezone.timedelta(days=1),
            seating_mode=Performance.SeatingMode.GA,
            status=Performance.Status.PUBLISHED,
        )
        GAAllocation.objects.create(organization=self.org, performance=performance, capacity=10)
        PriceTier.objects.create(
            organization=self.org, performance=performance, name="GA", amount=Decimal("20.00")
        )

    def test_platform_host_shows_landing_not_any_tenant_catalog(self):
        resp = self.client.get("/")
        self.assertIsNone(resp.wsgi_request.organization)
        self.assertContains(resp, "Boxo.show")
        self.assertNotContains(resp, "Roxy Season Opener")

    def test_platform_host_hides_admin_link_by_default(self):
        """Prod (SHOW_ADMIN_LINK unset/False): the public marketing landing
        page advertises no /admin/ link, even though /admin/ still works."""
        resp = self.client.get("/")
        self.assertNotContains(resp, 'href="/admin/"')

    @override_settings(SHOW_ADMIN_LINK=True)
    def test_platform_host_shows_admin_link_when_enabled(self):
        """Staging/beta sets SHOW_ADMIN_LINK=true so the platform-host landing
        page surfaces a convenience link to /admin/."""
        resp = self.client.get("/")
        self.assertContains(resp, 'href="/admin/"')

    def test_real_tenant_subdomain_resolves_to_its_org(self):
        other = make_org("globe")
        resp = self.client.get("/", HTTP_HOST="globe.localhost")
        self.assertEqual(resp.wsgi_request.organization, other)

    def test_admin_reachable_on_platform_host(self):
        resp = self.client.get("/admin/login/")
        self.assertEqual(resp.status_code, 200)

    def test_unmatched_url_renders_branded_404(self):
        resp = self.client.get("/this-page-does-not-exist/")
        self.assertEqual(resp.status_code, 404)
        self.assertContains(resp, "Page not found", status_code=404)

    def test_dev_tenant_override_resolves_tenant_when_debug(self):
        """The DEBUG-only ?_tenant=/X-Tenant override lets a laptop hit a
        tenant without per-tenant /etc/hosts entries; never active in prod."""
        other = make_org("globe")
        with override_settings(DEBUG=True):
            resp = self.client.get("/", HTTP_X_TENANT="globe")
        self.assertEqual(resp.wsgi_request.organization, other)


# Records the timezone that was active mid-request. Installed just after
# TenantMiddleware by the tests below so they can observe the active zone
# without depending on any particular template rendering a datetime.
_PROBE = {}


def _probe_zone_middleware(get_response):
    def middleware(request):
        _PROBE["zone"] = timezone.get_current_timezone_name()
        return get_response(request)

    return middleware


def _with_probe():
    from django.conf import settings

    mw = list(settings.MIDDLEWARE)
    mw.insert(
        mw.index("tenants.middleware.TenantMiddleware") + 1,
        "tenants.tests._probe_zone_middleware",
    )
    return mw


@override_settings(TIME_ZONE="UTC")
class TenantTimezoneActivationTests(TestCase):
    """Datetimes are stored UTC-aware, but a showtime is a fact about a place:
    every time renders in the tenant's own timezone (TenantMiddleware activates
    Organization.timezone), so "8:00 PM at the Roxy" shows as 8pm ET for every
    visitor. The platform host and unusable zone strings fall back to
    settings.TIME_ZONE, and the active zone never leaks between requests."""

    def setUp(self):
        _PROBE.clear()

    def _zone_during(self, host):
        with override_settings(MIDDLEWARE=_with_probe()):
            self.client.get("/", HTTP_HOST=host)
        return _PROBE.get("zone")

    def test_tenant_request_activates_venue_timezone(self):
        make_org("roxy", timezone="America/New_York")
        self.assertEqual(self._zone_during("roxy.localhost"), "America/New_York")

    def test_platform_host_falls_back_to_settings_timezone(self):
        make_org("roxy", timezone="America/New_York")
        # Bare host (Host: testserver) -> platform host, no tenant -> default.
        self.assertEqual(self._zone_during("testserver"), "UTC")

    def test_bad_timezone_string_falls_back_instead_of_500(self):
        make_org("roxy", timezone="Amerca/New_York")  # typo, not a real zone
        # Request still succeeds; zone falls back rather than crashing.
        self.assertEqual(self._zone_during("roxy.localhost"), "UTC")

    def test_blank_timezone_falls_back(self):
        make_org("roxy", timezone="")
        self.assertEqual(self._zone_during("roxy.localhost"), "UTC")

    def test_zone_does_not_leak_across_requests(self):
        make_org("roxy", timezone="America/New_York")
        self.client.get("/", HTTP_HOST="roxy.localhost")
        # The worker thread is reused; the next request must not inherit the
        # Roxy's zone (the middleware deactivates in a finally).
        self.assertEqual(timezone.get_current_timezone_name(), "UTC")

    def test_storefront_renders_showtime_in_venue_timezone(self):
        """End-to-end: the actual rendered showtime string is the venue-local
        time, not UTC. 2027-01-15 02:00 UTC is 2027-01-14 21:00 in New York."""
        org = make_org("roxy", timezone="America/New_York")
        venue = Venue.objects.create(organization=org, name="Main Stage")
        event = Event.objects.create(
            organization=org,
            title="Midnight Matinee",
            slug="midnight-matinee",
            status=Event.Status.PUBLISHED,
        )
        performance = Performance.objects.create(
            organization=org,
            event=event,
            venue=venue,
            starts_at=datetime(2027, 1, 15, 2, 0, tzinfo=ZoneInfo("UTC")),
            seating_mode=Performance.SeatingMode.GA,
            status=Performance.Status.PUBLISHED,
        )
        GAAllocation.objects.create(organization=org, performance=performance, capacity=10)
        PriceTier.objects.create(
            organization=org, performance=performance, name="GA", amount=Decimal("20.00")
        )

        resp = self.client.get("/", HTTP_HOST="roxy.localhost")
        self.assertContains(resp, "Thu, Jan 14 2027 — 9:00 PM")  # ET, not 2:00 AM UTC
        self.assertNotContains(resp, "2:00 AM")
