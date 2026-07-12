# Boxoffice — Project State, Gaps & Recommendations

_Audit date: 2026-07-12 · branch `claude/project-audit-gaps-hnv49q` · commit `8378604`_

## 1. Executive summary

Boxoffice is a **substantially complete, well-engineered** multi-tenant Django
box-office SaaS. All six planned build phases (scaffold → domain models →
storefront → payments/tickets → dashboard/scanning → deploy) are implemented,
plus later additions the spec didn't originally list: a seating-chart editor, a
role-based help center, a guest self-service portal, and pricing zones.

Health signals are strong:

- **603 tests pass** (`pytest -m "not multiprocess_concurrency"`), plus **2
  real multi-process concurrency tests** for double-booking (1 Postgres variant
  skipped only because no `POSTGRES_URL` was set locally).
- `manage.py check` and `makemigrations --check` are clean — **no missing
  migrations, no model drift.**
- `manage.py check --deploy` on `config.settings.prod` reports **0 issues**;
  prod settings fail fast on a weak/missing `SECRET_KEY`, force HTTPS/HSTS,
  secure cookies, and hardcode `DEBUG=False`.
- **Tenant isolation** (the spec's stated #1 risk) is clean: every URL-driven
  fetch of a tenant-scoped model is constrained by `request.organization`,
  directly or transitively through an already-scoped parent. QR ticket
  signatures and guest magic-links are HMAC/signed **per organization** and
  reject cross-tenant tokens.
- Security hygiene is good: `@csrf_exempt` appears only on the Stripe webhook
  (signature-verified), no raw SQL, no `eval`/`pickle`/`os.system`, no
  `mark_safe`.

The gaps below are real but bounded. **One is a launch-blocker** (a free-ticket
bypass); the rest are functional completeness, documentation drift, and the
usual "no-CI / no-observability" operational maturity items.

---

## 2. Gaps found (ranked)

### 🔴 CRITICAL — `checkout_stub` is an ungated free-ticket bypass on live tenants

`orders/views.py:363` (`checkout_stub`) fulfills a hold — creating real
`Order` + `Ticket`s with **no payment** (`provider="stub"`) — guarded by
*nothing* but a valid hold in the caller's own session (`@require_tenant`).
Its route `orders/urls.py:14` is registered unconditionally.

Contrast its sibling `checkout_test`, which fails closed:
`orders/views.py:328` → `if not settings.ENABLE_TEST_CHECKOUT: raise Http404`.
`checkout_stub` has **no `ENABLE_TEST_CHECKOUT` gate and no
`stripe_charges_enabled` gate.** `create_checkout_session`
(`payments/services.py:249`) only chooses the stub as the *redirect target*
when a tenant hasn't finished Connect onboarding — it does not protect the
endpoint.

**Impact:** a buyer on a **fully live** tenant (charges enabled) can add to
cart normally, then POST directly to `/checkout/stub/` with their own
`hold_id` + email and receive real tickets for free.

**Fix:** gate the view to only work when the tenant genuinely can't charge —
e.g. at the top of `checkout_stub`, `raise Http404` unless
`not request.organization.stripe_charges_enabled` (optionally also honoring
`ENABLE_TEST_CHECKOUT`). ~3 lines, mirrors the `checkout_test` guard.

### 🟠 HIGH — Order management actions are promised but not built

The built-in help center tells staff (`helpcenter/builtins.py:56,66,71`):
_"Orders — look up a buyer, **resend tickets, issue a refund** (box office)"_
and _"Look up an order, resend tickets, and process a refund."_

None of that exists in the UI. The dashboard order surface is **read-only**:
`dashboard/urls.py` exposes only `dashboard_order_list` and
`dashboard_order_detail`, and `templates/dashboard/order_detail.html` contains
**no `<form>` and no action button**. Concretely missing:

- **Refunds** — no `stripe.Refund.create` call anywhere in the codebase; the
  `Order.Status.REFUNDED` enum exists but is never set.
- **Void / cancel-and-reissue** — `Ticket.Status.VOID` and the
  `unique_live_ticket_per_performance_seat` constraint are built to *support*
  void-and-reissue (`orders/models.py:334`), but no view flips a ticket to
  `void`.
- **Resend confirmation email** — `send_ticket_email` exists
  (`orders/emails.py:18`) and is called at fulfillment, but nothing lets staff
  re-trigger it.

This is both a feature gap and a docs-vs-reality mismatch that will confuse
staff following the in-app help.

### 🟡 MEDIUM — Documentation drift

- **`README.md` is badly stale.** It states this is _"Phase 1 (scaffold)…
  No booking/payments/domain models yet"_ and _"No tests yet… Phase 1 is
  scaffolding"_ (README lines 9–13, 124–126). Reality: all phases shipped and
  603 tests pass. A new contributor reading the README gets an entirely wrong
  mental model. **Rewrite the phase/status framing.**
- **The `guests` app is undocumented.** A whole app — the buyer self-service
  portal with magic-link auth (`guests/urls.py`: `account/`, `.../link/`,
  `.../verify/`) — appears in neither `README.md` nor
  `docs/ARCHITECTURE.md`'s "Django apps" list.
- **Broken doc reference.** `tenants/management/commands/provision_tenant.py:7`
  and `docs/ARCHITECTURE.md` point to `docs/DEPLOY.md`, but the file lives at
  the repo **root** (`DEPLOY.md`).

### 🟡 MEDIUM — Uploaded media (tenant logos) won't be served in production

`Organization.logo` is an `ImageField` under `MEDIA_ROOT`
(`config/settings/base.py:143`), but:

- `config/urls.py:26` only serves media **when `DEBUG=True`**.
- WhiteNoise serves `STATIC_ROOT` only; `WHITENOISE_ROOT` is not set, so it
  won't serve `MEDIA_ROOT`.
- `deploy/nginx.sample.conf` is a plain proxy-to-port with **no `/media/`
  location block** (its comment even says static/media are "served by the
  app").

Net effect: a tenant that uploads a logo will get a broken image in prod.
Either add a `/media/` alias to the nginx sample, point WhiteNoise at the media
dir, or move uploads to object storage. (Color-based branding still works, so
this is degraded-not-broken.)

### 🟢 LOW — Payments correctness edge cases (from a focused Stripe audit)

Core payment flow is **verified correct**: webhook signature verification with
fail-closed empty-secret check, idempotency via a pre-check + the
`unique_stripe_checkout_session_per_org` partial unique constraint + a
nested-savepoint `IntegrityError` fallback, in-transaction hold re-validation
with `select_for_update`, correct `application_fee_amount` math, and
`account.updated` capability caching. Remaining edges:

1. **GA price can diverge from the amount charged.** `hold_total()` reads the
   tier price *live* (`orders/services.py:423`) while the Stripe line item was
   frozen at session creation. If staff edit `PriceTier.amount` mid-checkout,
   `Order.total`/`Payment.amount` record the new price while Stripe charged the
   old one. Reserved seats are immune (they snapshot `HoldSeat.unit_amount`);
   GA has no snapshot on the `Hold`. Narrow, staff-triggered, no buyer
   overcharge — but the recorded total is wrong. _Fix: snapshot GA unit amount
   onto the hold._
2. **Broad `except IntegrityError`** in `fulfill_checkout_session`
   (`payments/services.py:494`) conflates the duplicate-session race with a
   seat-conflict `IntegrityError`; the latter would 500 the webhook (Stripe
   retries for 3 days). Largely unreachable thanks to the `select_for_update`
   pre-check, but the catch should be narrowed.
3. **`_to_minor_units` assumes 2-decimal currencies** (`* 100`,
   `payments/services.py:330`). Wrong for zero-decimal (JPY) or 3-decimal
   currencies. Latent — only bites if a non-2-decimal `Organization.currency`
   is configured.
4. **Stub/test paths aren't idempotent** (rely on `hold.delete()`, no session
   unique constraint). Low on its own, but CRITICAL #1 makes the stub path
   production-reachable, which elevates it — another reason to gate the stub.

### 🟢 LOW — Operational maturity

- **No CI.** No `.github/` at all. `CLAUDE.md` acknowledges this
  ("no CI configured, nothing runs on the PR"), but with a 603-test suite and
  an autonomous merge model, a GitHub Actions workflow running
  `collectstatic` + `pytest` on PRs to `staging` is high-value, low-effort
  insurance.
- **No linter/formatter config.** No `ruff`/`black`/`flake8`/`pre-commit`
  config committed. Fine for a solo project; worth adding for consistency.
- **No error monitoring.** No Sentry, no `LOGGING` config in settings. In
  prod a 500 (e.g. a webhook that slips past the handled exceptions) is
  invisible. Add structured logging + an error reporter.
- **No login throttling / lockout** on the staff/email login (no
  `django-axes`/ratelimit). Storefront and dashboard both accept unlimited
  password attempts.
- **No `robots.txt` / `sitemap.xml` / favicon** for the public storefront —
  minor SEO/polish for a consumer-facing product.

---

## 3. Recommendations (prioritized)

**Before onboarding a real paying tenant:**

1. **Gate `checkout_stub`** on `stripe_charges_enabled` / `ENABLE_TEST_CHECKOUT`
   (CRITICAL — free-ticket bypass). Add a regression test asserting a
   charges-enabled tenant gets a 404.
2. **Serve media in prod** (nginx `/media/` alias or WhiteNoise media root) so
   tenant logos render.
3. **Snapshot GA unit price onto the hold** so `Order.total` always equals the
   amount charged.

**Before the next round of staff-facing work:**

4. **Build order-management actions** — resend tickets, refund (wire
   `stripe.Refund.create` + set `Order.status = REFUNDED`), and void/reissue —
   to match what the help center already promises. Or, short-term, edit the
   built-in help to stop promising unbuilt features.
5. **Fix documentation drift** — rewrite the README status framing, document
   the `guests` app in ARCHITECTURE, and fix the `docs/DEPLOY.md` → `DEPLOY.md`
   references.

**Operational hardening (any time):**

6. Add a **CI workflow** (collectstatic + pytest on PRs to `staging`).
7. Add **error monitoring + `LOGGING`** and **login throttling**.
8. Narrow the webhook `IntegrityError` catch; add a zero/3-decimal currency
   guard to the minor-units helper.

---

## 4. What's solid (keep doing)

Tenant isolation discipline, the atomic + `select_for_update` booking core with
real multi-process concurrency tests, fail-fast prod settings, per-tenant HMAC
ticket signing, idempotent webhook fulfillment, a clean two-chrome
storefront/dashboard separation, and a genuinely thorough deploy story
(`bin/boxoffice` operate CLI, hold sweeper via cron/systemd, pg_dump+media
backup). This is a codebase built with care.
