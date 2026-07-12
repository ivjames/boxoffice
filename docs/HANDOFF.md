# Boxoffice — Handoff / Delegation Plan

_Derived from `docs/AUDIT.md` (2026-07-12). Each item below is a
**self-contained work unit** sized for one delegated agent. Read the audit
entry it references, then execute — no re-audit needed._

## How to run each task (applies to every item)

Per `CLAUDE.md` (beta-first, autonomous):

1. `git fetch origin staging && git checkout -B <branch> origin/staging` —
   **cut from `staging`**, one branch per task.
2. Implement the change + its migration (if any) + tests.
3. Verify: `python manage.py collectstatic --noinput` first (WhiteNoise
   manifest), then run the **scoped** suite listed under the task — not the
   full suite. Then drive the real flow where the task has a runtime surface.
4. Open a PR **against `staging`** (never `main`) and self-merge — there is no
   human reviewer and no CI gate; correctness is your responsibility.

Effort key: **S** ≈ <1h, **M** ≈ half-day, **L** ≈ 1–2 days.
Tasks with no shared files can run in **parallel**; dependencies are noted.

---

## Wave 1 — Launch blockers (do first, can run in parallel)

### BO-1 · Gate `checkout_stub` (🔴 CRITICAL) — S
- **Problem:** `orders/views.py:363` fulfills a hold with no payment, guarded
  only by `@require_tenant`. Route `orders/urls.py:14` is always registered.
  A buyer on a charges-enabled tenant can POST `/checkout/stub/` with their own
  `hold_id` and get free tickets. (Audit §2 CRITICAL.)
- **Do:** At the top of `checkout_stub`, `raise Http404` unless the tenant
  genuinely can't charge — i.e. allow only when
  `not request.organization.stripe_charges_enabled` (mirror the
  `checkout_test` guard at `orders/views.py:328`). Consider also honoring
  `ENABLE_TEST_CHECKOUT` for symmetry.
- **Acceptance:** new regression test — a tenant with
  `stripe_charges_enabled=True` gets 404 from both GET and POST to
  `/checkout/stub/`; a not-yet-onboarded tenant still succeeds.
  `python manage.py test orders.test_views orders.test_checkout_test`.
- **Deps:** none. **Branch:** `claude/gate-checkout-stub`.

### BO-2 · Serve uploaded media in production (🟡 MEDIUM) — S/M
- **Problem:** tenant logos (`Organization.logo`, `MEDIA_ROOT`) are served only
  under `DEBUG` (`config/urls.py:26`); WhiteNoise serves static only; the nginx
  sample has no `/media/` block → broken logos in prod. (Audit §2 MEDIUM-media.)
- **Do:** pick one and document it in `DEPLOY.md`: (a) add a `/media/` alias
  location to `deploy/nginx.sample.conf`, **or** (b) point WhiteNoise at the
  media dir via `WHITENOISE_ROOT`, **or** (c) move uploads to object storage.
  Recommendation: (a) — smallest, matches the lab980 nginx model.
- **Acceptance:** document the manual verification (upload a logo on a tenant,
  confirm it renders over the prod path). No unit test needed if going the
  nginx route; if (b), add a settings assertion test.
- **Deps:** none. **Branch:** `claude/serve-prod-media`.

### BO-3 · Snapshot GA unit price onto the hold (🟢 LOW→correctness) — M
- **Problem:** `hold_total()` reads GA tier price *live* (`orders/services.py:423`)
  while the Stripe line item was frozen at session creation. Editing
  `PriceTier.amount` mid-checkout makes `Order.total`/`Payment.amount` disagree
  with the actual charge. Reserved seats already snapshot `HoldSeat.unit_amount`;
  GA has no equivalent. (Audit §2 LOW-1.)
- **Do:** add a GA unit-amount snapshot to `Hold` (migration), set it when the
  GA hold is created/refreshed, and have `hold_total()` + the Stripe line item
  read the snapshot for GA. Mirror the reserved-seat pattern.
- **Acceptance:** test that changing `PriceTier.amount` after hold creation does
  **not** change the recorded order total. `python manage.py test orders`.
- **Deps:** none, but touches `orders/services.py` / `orders/models.py` — avoid
  landing concurrently with BO-6. **Branch:** `claude/ga-price-snapshot`.

---

## Wave 2 — Order management (🟠 HIGH; epic — the help center already promises this)

> **Decision gate for the delegator:** if these won't be built soon, instead do
> the 15-minute **BO-4d** (trim the help text) so the app stops promising
> unbuilt features. Otherwise build BO-4a→c. All three share
> `dashboard/views.py` + `dashboard/urls.py` + `order_detail.html`, so run them
> **sequentially on one branch** (or as stacked commits), not in parallel.

Shared context: dashboard order surface is read-only today
(`dashboard/urls.py` has only list + detail; `order_detail.html` has no form).
Gate every new action behind box-office role (`accounts/permissions.py`).

### BO-4a · Resend ticket email — S
- **Do:** add a POST action + button on `order_detail.html` that calls the
  existing `send_ticket_email` (`orders/emails.py:18`). No model change.
- **Acceptance:** test that POSTing the action re-sends to the order's buyer and
  is role-gated. `python manage.py test dashboard orders.test_emails`.

### BO-4b · Void / cancel-and-reissue tickets — M
- **Do:** action to flip a `Ticket` to `Status.VOID` (the
  `unique_live_ticket_per_performance_seat` constraint at `orders/models.py:334`
  is already built to allow reissue). Decide + document whether reissue mints a
  replacement ticket. Free the seat/GA count on void.
- **Acceptance:** voided ticket stops counting as sold (dashboard tallies already
  exclude VOID — `dashboard/views.py:67,110`), seat becomes re-bookable, scanner
  rejects it. `python manage.py test dashboard orders scanning`.

### BO-4c · Refunds — M/L
- **Do:** action to refund via `stripe.Refund.create` on the connected account
  (reuse the platform-key + `stripe_account=` pattern in `payments/services.py`);
  set `Order.Status.REFUNDED`; decide refund↔void coupling (refunding usually
  voids the tickets). Handle the stub/test provider case (no Stripe refund —
  just mark refunded). Idempotency + error handling.
- **Acceptance:** refund path sets status, voids tickets, and is idempotent under
  a double-submit; unit-test with a mocked Stripe client (see existing
  `payments/test_services.py` for the mocking pattern).
  `python manage.py test payments dashboard orders`.

### BO-4d · (Alternative) Trim help center to match reality — S
- **Do:** if BO-4a–c are deferred, edit `helpcenter/builtins.py:56,66,71` to stop
  advertising resend/refund until they exist. `python manage.py test helpcenter`.

**Branch (epic):** `claude/order-management-actions`.

---

## Wave 3 — Documentation (🟡 MEDIUM; independent, parallel-safe)

### BO-5 · Fix documentation drift — S
- **Do, in one branch:**
  - Rewrite `README.md` status framing (lines ~9–13, 124–126) — it still claims
    "Phase 1 scaffold, no models/payments, no tests." All phases shipped; 603
    tests pass.
  - Add the **`guests`** app (buyer self-service portal, magic-link auth) to the
    "Django apps" list in `docs/ARCHITECTURE.md` and README.
  - Fix `docs/DEPLOY.md` references → root `DEPLOY.md`
    (`tenants/management/commands/provision_tenant.py:7`, `docs/ARCHITECTURE.md`).
- **Acceptance:** `grep -rn "docs/DEPLOY.md"` returns nothing; README no longer
  says "Phase 1"/"no tests"; ARCHITECTURE lists `guests`. Docs-only — no tests.
- **Deps:** none. **Branch:** `claude/fix-doc-drift`.

---

## Wave 4 — Payments hardening (🟢 LOW; parallel-safe with docs, coordinate with BO-3)

### BO-6 · Narrow webhook error handling + currency guard — S/M
- **Do:**
  - Narrow the broad `except IntegrityError` in `fulfill_checkout_session`
    (`payments/services.py:494`) so a seat-conflict `IntegrityError` maps to a
    handled `AvailabilityChangedError` instead of 500-ing the webhook (Stripe
    then retries for 3 days). (Audit §2 LOW-2.)
  - Add a zero-/3-decimal-currency guard to `_to_minor_units`
    (`payments/services.py:330`) — it assumes `*100`. (Audit §2 LOW-3.)
- **Acceptance:** test that a concurrent seat-conflict during fulfillment returns
  a handled response (not a 500) and that JPY-style currencies compute correct
  minor units. `python manage.py test payments`.
- **Deps:** shares `payments/services.py` with BO-3 — sequence after BO-3 or
  rebase. **Branch:** `claude/payments-hardening`.

---

## Wave 5 — Operational maturity (🟢 LOW; all independent, parallel-safe)

### BO-7 · CI workflow — S
- **Do:** add `.github/workflows/ci.yml` running on PRs to `staging`:
  install deps → `collectstatic --noinput` → `pytest -m "not multiprocess_concurrency"`
  (optionally a second job for the concurrency marker). No secrets needed (dev
  settings, SQLite).
- **Acceptance:** workflow green on a throwaway PR. **Branch:** `claude/add-ci`.

### BO-8 · Error monitoring + logging — M
- **Do:** add a `LOGGING` config to `config/settings/prod.py` and an error
  reporter (e.g. Sentry via `SENTRY_DSN` env, off when unset). Document the env
  var in `.env.example`.
- **Acceptance:** unset DSN = no-op (no import error, no network); check --deploy
  still clean. **Branch:** `claude/error-monitoring`.

### BO-9 · Login throttling — M
- **Do:** add lockout/throttling on staff + guest login (e.g. `django-axes` or a
  cache-based rate limit). Pin the dep, migrate if needed.
- **Acceptance:** test that N failed logins locks/430s the attempt.
  `python manage.py test accounts guests`. **Branch:** `claude/login-throttle`.

### BO-10 · Storefront polish: robots/sitemap/favicon — S
- **Do:** add `robots.txt`, a `sitemap.xml` (per-tenant events), and a favicon.
- **Acceptance:** routes return 200 on a tenant host. **Branch:** `claude/storefront-seo`.

---

## Suggested delegation order

| Wave | Tasks | Parallel? | Gate before shipping to `main` |
|------|-------|-----------|-------------------------------|
| 1 | BO-1, BO-2, BO-3 | yes (disjoint files) | **BO-1 is required** |
| 2 | BO-4 epic (or BO-4d) | sequential within epic | decision gate first |
| 3 | BO-5 | yes | — |
| 4 | BO-6 | after BO-3 (shared file) | — |
| 5 | BO-7, BO-8, BO-9, BO-10 | yes | — |

**Minimum bar before onboarding a real paying tenant:** BO-1 (blocker), BO-2,
BO-3. Everything else is quality/completeness and can follow on `staging`.
