"""Scanning: redeem flow correctness (pass/fail reasons, double-scan,
signature/tenant checks), the lock-then-recheck concurrency guarantee, and
role/tenant gating on both /scan/ and /S/<token>/<sig>/.

Concurrency-test caveat (same as orders/test_services.py's
GAAvailabilityTests.test_concurrent_last_tickets_only_one_succeeds): SQLite
test runs use an in-memory DB per connection, so real background threads
wouldn't share state. redeem_ticket() already wraps its own
transaction.atomic()+select_for_update() per call, so calling it twice in a
row for the same ticket exercises the exact interleaving harden_sqlite()
(transaction_mode=IMMEDIATE) and Postgres select_for_update() both guarantee
in production: the first call's commit is what the second call's re-read
inside its own lock sees -- "first to commit wins."
"""

from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from accounts.models import Membership, User
from accounts.tests import StaffFixtureMixin, host_for
from events.models import Event, Performance
from orders.models import Order, Ticket
from orders.tokens import sign_token
from venues.models import Venue
from venues.tests import make_org

from .services import redeem_ticket


class ScanFixtureMixin:
    def build_org_with_ticket(self, subdomain="roxy"):
        org = make_org(subdomain)
        venue = Venue.objects.create(organization=org, name="Main Stage")
        event = Event.objects.create(
            organization=org, title="Show", slug="show", status=Event.Status.PUBLISHED
        )
        performance = Performance.objects.create(
            organization=org,
            event=event,
            venue=venue,
            starts_at=timezone.now() + timedelta(days=1),
            seating_mode=Performance.SeatingMode.GA,
            status=Performance.Status.PUBLISHED,
        )
        order = Order.objects.create(
            organization=org,
            performance=performance,
            buyer_email="buyer@example.com",
            total=Decimal("20.00"),
            status=Order.Status.PAID,
        )
        ticket = Ticket.objects.create(
            organization=org, order=order, performance=performance, holder_name="Buyer Person"
        )
        return org, venue, event, performance, order, ticket


class RedeemTicketServiceTests(ScanFixtureMixin, TestCase):
    """Direct service-level tests -- see scanning/services.py."""

    def setUp(self):
        self.org, self.venue, self.event, self.performance, self.order, self.ticket = (
            self.build_org_with_ticket()
        )
        self.scanner_user, _ = StaffFixtureMixin().make_staff(self.org, Membership.Role.SCANNER)
        self.sig = sign_token(self.ticket.token, self.org.id)

    def test_valid_ticket_redeems(self):
        result = redeem_ticket(
            organization=self.org, token=self.ticket.token, sig=self.sig, scanned_by=self.scanner_user
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.reason, "ok")
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.status, Ticket.Status.USED)
        self.assertEqual(self.ticket.scanned_by_id, self.scanner_user.id)
        self.assertIsNotNone(self.ticket.used_at)

    def test_double_scan_rejected(self):
        first = redeem_ticket(
            organization=self.org, token=self.ticket.token, sig=self.sig, scanned_by=self.scanner_user
        )
        self.assertTrue(first.ok)

        other_scanner, _ = StaffFixtureMixin().make_staff(
            self.org, Membership.Role.SCANNER, email="other-scanner@roxy.example.com"
        )
        second = redeem_ticket(
            organization=self.org, token=self.ticket.token, sig=self.sig, scanned_by=other_scanner
        )
        self.assertFalse(second.ok)
        self.assertEqual(second.reason, "already_used")
        self.assertIn(self.scanner_user.email, second.message)

        self.ticket.refresh_from_db()
        # Still attributed to whoever scanned it FIRST, not the second attempt.
        self.assertEqual(self.ticket.scanned_by_id, self.scanner_user.id)

    def test_concurrent_redeems_only_one_succeeds(self):
        """Two 'simultaneous' redemption attempts for the same valid ticket
        -- only one can ever end up ok=True. See module docstring for why
        this is simulated sequentially rather than threaded."""
        results = [
            redeem_ticket(
                organization=self.org, token=self.ticket.token, sig=self.sig, scanned_by=self.scanner_user
            )
            for _ in range(2)
        ]
        oks = [r.ok for r in results]
        self.assertEqual(oks.count(True), 1)
        self.assertEqual(oks.count(False), 1)
        self.assertEqual(Ticket.objects.get(pk=self.ticket.pk).status, Ticket.Status.USED)

    def test_void_ticket_rejected(self):
        self.ticket.status = Ticket.Status.VOID
        self.ticket.save(update_fields=["status"])
        result = redeem_ticket(
            organization=self.org, token=self.ticket.token, sig=self.sig, scanned_by=self.scanner_user
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "void")
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.status, Ticket.Status.VOID)  # unchanged

    def test_missing_sig_rejected(self):
        result = redeem_ticket(
            organization=self.org, token=self.ticket.token, sig="", scanned_by=self.scanner_user
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "bad_sig")
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.status, Ticket.Status.VALID)  # untouched

    def test_tampered_sig_rejected(self):
        result = redeem_ticket(
            organization=self.org, token=self.ticket.token, sig="0" * 64, scanned_by=self.scanner_user
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "bad_sig")

    def test_unknown_token_not_found(self):
        import uuid

        unknown_token = uuid.uuid4()
        result = redeem_ticket(
            organization=self.org,
            token=unknown_token,
            sig=sign_token(unknown_token, self.org.id),
            scanned_by=self.scanner_user,
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "not_found")

    def test_ticket_from_another_org_rejected(self):
        """A ticket token + a sig that's valid for a DIFFERENT org must not
        redeem -- both the signature (HMAC keyed per-org, see
        orders/tokens.py) and the Ticket lookup itself are org-scoped, so
        this is defense in depth against either check alone failing."""
        other_org, _venue, _event, _perf, _order, other_ticket = self.build_org_with_ticket("otherorg")
        other_sig = sign_token(other_ticket.token, other_org.id)

        # Right sig, wrong org passed to redeem_ticket.
        result = redeem_ticket(
            organization=self.org, token=other_ticket.token, sig=other_sig, scanned_by=self.scanner_user
        )
        self.assertFalse(result.ok)
        self.assertIn(result.reason, ("bad_sig", "not_found"))
        other_ticket.refresh_from_db()
        self.assertEqual(other_ticket.status, Ticket.Status.VALID)


class ScanRedeemViewTests(ScanFixtureMixin, TestCase):
    def setUp(self):
        self.org, self.venue, self.event, self.performance, self.order, self.ticket = (
            self.build_org_with_ticket()
        )
        self.sig = sign_token(self.ticket.token, self.org.id)
        self.url = f"/S/{self.ticket.token}/{self.sig}/"

    def test_all_staff_roles_can_redeem(self):
        """Membership.can_scan() intentionally covers every role -- 'every
        role can work the door, not just dedicated scanners' per
        accounts/models.py -- so all four roles reach the endpoint. Each
        uses its own fresh ticket since redemption is one-shot."""
        for role in [
            Membership.Role.OWNER,
            Membership.Role.MANAGER,
            Membership.Role.BOX_OFFICE,
            Membership.Role.SCANNER,
        ]:
            user, _ = StaffFixtureMixin().make_staff(self.org, role, email=f"{role}@roxy.example.com")
            order = Order.objects.create(
                organization=self.org,
                performance=self.performance,
                buyer_email="x@example.com",
                total=Decimal("20.00"),
                status=Order.Status.PAID,
            )
            ticket = Ticket.objects.create(organization=self.org, order=order, performance=self.performance)
            sig = sign_token(ticket.token, self.org.id)

            self.client.logout()
            self.client.force_login(user)
            resp = self.client.get(
                f"/S/{ticket.token}/{sig}/", HTTP_HOST=host_for("roxy")
            )
            self.assertEqual(resp.status_code, 200, f"{role} should be able to redeem")
            self.assertContains(resp, "PASS")

    def test_html_result_page_shows_pass(self):
        user, _ = StaffFixtureMixin().make_staff(self.org, Membership.Role.SCANNER)
        self.client.force_login(user)
        resp = self.client.get(self.url, HTTP_HOST=host_for("roxy"))
        self.assertContains(resp, "PASS")
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.status, Ticket.Status.USED)

    def test_second_scan_shows_fail_already_used(self):
        user, _ = StaffFixtureMixin().make_staff(self.org, Membership.Role.SCANNER)
        self.client.force_login(user)
        self.client.get(self.url, HTTP_HOST=host_for("roxy"))
        resp = self.client.get(self.url, HTTP_HOST=host_for("roxy"))
        # An already-redeemed ticket gets its own "already scanned" verdict
        # (amber), distinct from a hard reject -- see scan_result.html.
        self.assertContains(resp, "ALREADY SCANNED")
        self.assertContains(resp, "Already scanned")

    def test_json_response_for_fetch_style_request(self):
        user, _ = StaffFixtureMixin().make_staff(self.org, Membership.Role.SCANNER)
        self.client.force_login(user)
        resp = self.client.get(self.url, HTTP_HOST=host_for("roxy"), HTTP_ACCEPT="application/json")
        self.assertEqual(resp["Content-Type"], "application/json")
        data = resp.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["ticket"]["status"], "used")

    def test_anonymous_redirected_to_login(self):
        resp = self.client.get(self.url, HTTP_HOST=host_for("roxy"))
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login/", resp.headers["Location"])

    def test_user_with_no_membership_forbidden(self):
        user = User.objects.create_user(email="nobody@example.com", password="pw12345!")
        self.client.force_login(user)
        resp = self.client.get(self.url, HTTP_HOST=host_for("roxy"))
        self.assertEqual(resp.status_code, 403)
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.status, Ticket.Status.VALID)

    def test_membership_in_a_different_org_forbidden(self):
        other_org = make_org("otherorg")
        user, _ = StaffFixtureMixin().make_staff(other_org, Membership.Role.SCANNER)
        self.client.force_login(user)
        resp = self.client.get(self.url, HTTP_HOST=host_for("roxy"))
        self.assertEqual(resp.status_code, 403)
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.status, Ticket.Status.VALID)

    def test_platform_host_404s(self):
        user, _ = StaffFixtureMixin().make_staff(self.org, Membership.Role.SCANNER)
        self.client.force_login(user)
        resp = self.client.get(self.url)  # no tenant host
        self.assertEqual(resp.status_code, 404)


class ScanHomeViewTests(ScanFixtureMixin, TestCase):
    def setUp(self):
        self.org, self.venue, self.event, self.performance, self.order, self.ticket = (
            self.build_org_with_ticket()
        )
        self.scanner_user, _ = StaffFixtureMixin().make_staff(self.org, Membership.Role.SCANNER)

    def test_scan_home_requires_login(self):
        resp = self.client.get("/scan/", HTTP_HOST=host_for("roxy"))
        self.assertEqual(resp.status_code, 302)

    def test_scan_home_renders_for_scanner(self):
        self.client.force_login(self.scanner_user)
        resp = self.client.get("/scan/", HTTP_HOST=host_for("roxy"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Scan tickets")
        self.assertContains(resp, "qrScanner()")

    def test_manual_entry_redeems_valid_ticket(self):
        self.client.force_login(self.scanner_user)
        resp = self.client.post(
            "/scan/", {"token": str(self.ticket.token)}, HTTP_HOST=host_for("roxy")
        )
        self.assertEqual(resp.status_code, 302)
        self.assertIn(f"/S/{self.ticket.token}/", resp.headers["Location"])

        follow = self.client.get(resp.headers["Location"], HTTP_HOST=host_for("roxy"))
        self.assertContains(follow, "PASS")
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.status, Ticket.Status.USED)

    def test_manual_entry_lowercase_token_is_normalized(self):
        # Staff may type/paste the code in lowercase; it's upper()ed before use.
        self.client.force_login(self.scanner_user)
        resp = self.client.post(
            "/scan/", {"token": str(self.ticket.token).lower()}, HTTP_HOST=host_for("roxy")
        )
        self.assertEqual(resp.status_code, 302)
        self.assertIn(f"/S/{self.ticket.token}/", resp.headers["Location"])

    def test_manual_entry_rejects_garbage_token(self):
        # Tokens are uppercase base32 (A-Z2-7), so a "wrong" code is rejected at
        # input only if it has chars outside that alphabet (a space, punctuation,
        # or 0/1/8/9); a well-formed-but-unknown code is handled below instead.
        self.client.force_login(self.scanner_user)
        resp = self.client.post("/scan/", {"token": "bad token!"}, HTTP_HOST=host_for("roxy"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "doesn&#x27;t look like a valid ticket code")

    def test_manual_entry_of_well_formed_unknown_token_reaches_redeem(self):
        # A syntactically valid but nonexistent token isn't rejected up front;
        # it flows through to scan_redeem/redeem_ticket -- the single place that
        # decides pass/fail -- which reports it as not found.
        self.client.force_login(self.scanner_user)
        resp = self.client.post("/scan/", {"token": "ABCDEFG234567AB"}, HTTP_HOST=host_for("roxy"))
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/S/ABCDEFG234567AB/", resp.headers["Location"])
        follow = self.client.get(resp.headers["Location"], HTTP_HOST=host_for("roxy"))
        self.assertContains(follow, "FAIL")

    def test_platform_host_404s(self):
        self.client.force_login(self.scanner_user)
        resp = self.client.get("/scan/")
        self.assertEqual(resp.status_code, 404)
