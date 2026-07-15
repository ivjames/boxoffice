# Boxoffice — Architecture & Build Spec

White-label, multi-tenant theater box office SaaS. One Django deployment serves
many branded theaters, each on its own subdomain. Public storefront (browse →
buy → Stripe checkout → emailed QR ticket) plus a staff dashboard (manage
events, view orders, scan tickets at the door).

This document is the single source of truth. Every build agent reads it before
writing code and must not deviate without flagging the orchestrator.

## Stack

- Python 3.11+, Django 5.2 LTS
- Database via `DATABASE_URL` (django-environ). **Default deployment = SQLite in
  the app-dir `data/db.sqlite3`** to match the lab980 one-dir-per-site model
  (config + data in the app dir, no extra infra). `harden_sqlite()` in
  `config/settings/base.py` sets `transaction_mode=IMMEDIATE` (acquire the write
  lock at BEGIN, needs Django 5.1+) + a busy `timeout`, so concurrent checkouts
  serialize and wait instead of racing/erroring — seat/GA double-booking is
  impossible when every booking mutation runs in `transaction.atomic()` and
  re-checks availability. Postgres is a drop-in upgrade (set `DATABASE_URL` to a
  `postgres://` URL, nothing else changes); `psycopg` stays in requirements so
  it's one env var away. Use `select_for_update()` in booking code regardless —
  a no-op on SQLite, real row locking on Postgres.
- Static files via **WhiteNoise** (served by the app, not nginx) so the lab980
  nginx vhost stays a plain proxy-to-port with zero per-app location blocks.
- Server-rendered Django templates. No JS build step for MVP.
  - Styling: Tailwind via the standalone CLI (no Node app dependency) OR a small
    committed CSS file. Prefer a single committed `static/css/app.css` to keep
    deploys simple; use CSS custom properties for per-tenant theming.
  - Interactivity: Alpine.js (single CDN/vendored file) for the cart and the
    reserved-seat picker. Avoid a bundler.
- Payments: Stripe Connect (Express) + Checkout Sessions + webhooks. boxo.show
  is the platform account; each theater is a connected account (direct charges,
  merchant-of-record = the theater) with an optional platform application fee.
  Platform keys live in settings; only the connected-account id sits on the
  Organization. See "Payments" below.
- QR: `segno` (pure-python, no C deps). Email: Django SMTP backend.
- Deploy target: lab980 droplet, gunicorn under pm2, nginx proxy, `/var/www/boxoffice`.
  gunicorn binds `127.0.0.1:$PORT` where PORT comes from the app-dir `.env`
  (seeded by lab980 `provision-site`). Prod settings set
  `SECURE_PROXY_SSL_HEADER=('HTTP_X_FORWARDED_PROTO','https')`,
  `ALLOWED_HOSTS`/`CSRF_TRUSTED_ORIGINS` covering `.boxo.show`.

## Multi-tenancy

Shared-schema, row-level tenancy (NOT schema-per-tenant). Simpler ops, fine for MVP.

- `Organization` (a.k.a. tenant/theater): `name`, `slug`, `subdomain` (unique),
  branding (`logo`, `primary_color`, `accent_color`), `timezone`, `currency`,
  Stripe Connect fields (`stripe_account_id`, cached `stripe_charges_enabled` /
  `stripe_details_submitted`, optional `platform_fee_percent` override), contact
  email, `is_active`.
- `TenantMiddleware` resolves the Organization from the request Host header
  (subdomain). Attaches `request.organization`. Unknown/inactive subdomain → 404.
  A reserved subdomain (`www`, `app`, `admin`, none) serves the marketing/landing +
  tenant signup + platform staff area.
- **Subdomain onboarding matches the lab980 model — no wildcard.** Each tenant
  gets a real `<sub>.boxo.show` provisioned the lab980 way: one DNS A record
  (doctl) + one nginx vhost + one per-site certbot cert. The trick: every tenant
  vhost proxies to the SAME boxoffice gunicorn port; the middleware discriminates
  by Host. Onboarding a theater = create the Organization row + run the lab980
  provisioning pointed at the existing port. Phase 6 ships
  `bin/boxoffice add-tenant <sub>` that does exactly this. A wildcard cert
  (DNS-01) is an optional alternative, NOT required and NOT the default.
- EVERY tenant-scoped model carries `organization = ForeignKey(Organization)`.
  All storefront/staff querysets MUST filter by `request.organization`. Provide a
  `TenantScopedManager` and a base view mixin that enforces this — cross-tenant
  data leakage is the #1 risk. Add DB indexes on `(organization, ...)`.

## Django apps

- `tenants` — Organization, TenantMiddleware, branding, tenant signup/onboarding.
- `accounts` — custom `User` (email login), `Membership(user, organization, role)`.
  Roles: `owner`, `manager`, `box_office`, `scanner`. Permission helpers per role.
- `venues` — `Venue`, `SeatingChart`, `Section`, `Seat` (row/number/x/y for the map).
- `events` — `Event` (a production/show: title, description, images, category),
  `Performance` (a single dated showing: `event`, `venue`, `starts_at`,
  `seating_mode` = `GA` | `RESERVED`, `status`), `PriceTier`
  (name, amount, applies to sections or the whole GA performance),
  `GAAllocation` (capacity + sold count for GA performances).
- `orders` — `Cart`/`Hold`, `Order`, `OrderItem`, `Ticket`, `Payment`.
- `payments` — Stripe checkout session creation + webhook handling (per tenant).
- `scanning` — ticket validation + redemption endpoint and staff scan UI.
- `guests` — buyer self-service portal: a returning ticket buyer signs in with
  a per-tenant magic link (no password) and sees every order they've placed at
  this theater. `GuestAccount` is keyed off the buyer's email at checkout
  fulfillment; the portal is tenant-scoped like the rest of the storefront.
- `helpcenter` — tenant-authored knowledge base + built-in FAQ, surfaced to
  staff (role-filtered, in the dashboard) and buyers (public storefront FAQ).
  See `docs/HELP.md`.

## Key data & flows

### Help center

- `HelpArticle` (tenant-scoped) is a manager-authored article — house rules,
  show info, policies, how-tos. Its `visibility` maps onto the `accounts`
  role hierarchy: `public` > `staff` > `box_office` > `manager` > `owner`,
  cumulative in the same direction roles are. `public` articles also appear on
  the storefront FAQ.
- `HelpArticle.objects.readable_by(org, membership)` (staff) and `.public(org)`
  (storefront) are the ONLY places visibility → queryset filtering happens.
- A small set of read-only **built-in** articles (`helpcenter/builtins.py`)
  ships as a fallback so Help/FAQ is useful before a manager writes anything;
  built-ins are filtered by the same visibility rules and merged into the same
  category groups as authored content. Full detail in `docs/HELP.md`.

### Seating

- GA performance: `GAAllocation(performance, capacity, sold)`. A hold reserves N
  by atomically checking `sold + held + N <= capacity` inside a
  `transaction.atomic()` with `select_for_update()` on the allocation row.
- Reserved performance: availability is per-`Seat` per-`Performance`. A `Ticket`
  or active `Hold` on `(performance, seat)` means unavailable. Seat selection uses
  `select_for_update()` on the seat's availability rows inside a transaction.

### Hold / cart lifecycle

`Hold` has `expires_at` (default now + 10 min), `organization`, `performance`,
selected seats or GA qty, and a `session_key`/user. Expired holds are ignored in
availability math; a periodic sweeper (management command run by cron/pm2) deletes
them. Holds convert to `Order` on successful payment.

### Checkout

1. Cart → create/refresh `Hold`. 2. POST checkout → create a Stripe Checkout
Session as a **direct charge on the theater's connected account** (platform key
+ `stripe_account=<acct_id>`), with the platform's cut as `application_fee_amount`
(0 by default → omitted), `success_url`/`cancel_url` on the tenant subdomain,
metadata = hold id. A theater that hasn't finished Connect onboarding
(`stripe_charges_enabled` False) falls back to a simulated stub checkout so the
pre-launch demo flow still works. 3. **One platform Connect webhook**
(`checkout.session.completed`) → verify signature against the single
`settings.STRIPE_WEBHOOK_SECRET` → resolve the theater from the event's
top-level `account` (→ `stripe_account_id`) → within a transaction: re-validate
the hold, create `Order` + `Ticket`s (each with a signed QR token), mark
seats/GA sold, delete the hold → email tickets. Idempotent on session id.
`account.updated` events keep each Organization's cached capability flags fresh.

### Payments (Stripe Connect, Express)

boxo.show is the platform Stripe account; each theater onboards as a **connected
Express account** via an in-app flow (owner-only, `billing_required`):
`Account.create(type="express")` → `AccountLink` → Stripe-hosted onboarding →
return view + `account.updated` webhook cache `charges_enabled`/
`details_submitted` onto the Organization. Charges are **direct** (theater is
merchant of record — its name on the buyer's statement, it bears its own Stripe
fees and disputes); the platform takes a cut via `application_fee_amount`, sized
by `PLATFORM_FEE_PERCENT` + `PLATFORM_FEE_FIXED_CENTS` (both default 0 = no cut)
with a per-theater `Organization.platform_fee_percent` override. There are no
per-tenant Stripe keys — the one platform key selects a theater per call with
the `stripe_account` request option. See `payments/services.py` + `payments/views.py`.

### Ticket & scanning

- `Ticket`: `order`, `performance`, `seat` (nullable for GA), `holder_name`,
  `token` (10-char code, ~47 bits, from an unambiguous uppercase-alphanumeric
  alphabet with look-alike glyphs like 0/O·1/I/L·5/S·2/Z removed — short and
  safe to key in by hand on manual entry; see `orders.models.new_token`),
  `status` (`valid`|`used`|`void`), `used_at`, `scanned_by`.
- QR encodes a bare code `<token>.<sig>` (no URL) where `sig` = the first 96
  bits of an HMAC of the token with the tenant/app secret, base32-encoded
  (`orders/tokens.py`). Both halves are uppercase base32, so the code sits in
  QR *alphanumeric mode* — ~45% denser per module than byte mode — and it
  stays sparse even at the highest error-correction level (`error="h"`, ~30%).
  The in-page scanner (`static/js/scanner.js`) decodes the code, splits it, and
  calls the internal redeem endpoint `/S/<token>/<sig>/` itself; the scan view
  (role `scanner`+) verifies sig, checks status, atomically flips
  `valid`→`used`, returns pass/fail UI. Trade-off: because the QR is not a URL,
  a phone's stock camera app can't open it — redemption goes through the staff
  scanner UI only.

## URL structure (per tenant subdomain)

- `/` storefront home (upcoming events)
- `/events/<event-slug>/` event + performance list
- `/performances/<id>/` seat/qty selection
- `/cart/`, `/checkout/`, `/checkout/success/`, `/checkout/cancel/`
- `/tickets/<order-token>/` order confirmation + tickets
- `/faq/` public storefront help/FAQ (public articles + built-ins)
- `/dashboard/` staff area (events CRUD, orders, reports)
- `/dashboard/help/` staff help (role-filtered); `/dashboard/help/manage/`,
  `/dashboard/help/new/`, `/dashboard/help/<id>/edit/`, `.../delete/` (manager+)
- `/scan/` scanner UI, `/S/<token>/<sig>/` (internal redeem endpoint; the QR
  encodes a bare `<token>.<sig>` code, not this URL)
- `/webhooks/stripe/` tenant Stripe webhook
- Platform/landing (reserved host): `/`, `/signup/`, Django `/admin/`

## Conventions for build agents

- Settings split: `config/settings/{base,dev,prod}.py`, secrets via env
  (`django-environ`), `.env.example` committed, `.env` gitignored.
- `requirements.txt` + `requirements-dev.txt`. Pin major versions.
- Every model change ships its migration. Register key models in Django admin.
- Write focused tests for money/seat-locking/tenant-isolation paths (pytest-django).
- Tenant isolation is non-negotiable: never query tenant data without the org filter.
- Keep templates under each app's `templates/<app>/`; shared base in `templates/base.html`
  with branding via CSS variables from `request.organization`.
- **Two chromes, kept separate.** Public/buyer pages (storefront, `/faq/`)
  extend `base.html` and show the storefront menu. Internal staff pages
  (dashboard, help center, scan result) extend `templates/dashboard/base.html`,
  which drops that menu (`site_header` block emptied) so the consumer nav never
  bleeds into the staff area; internal navigation is the dashboard section nav
  (`templates/dashboard/_nav.html`), whose single "View storefront" link is the
  one explicit way back to the main site. New staff pages extend
  `dashboard/base.html`, not `base.html`.
- Deployment: `bin/boxoffice` operate CLI (deploy/restart/logs/migrate/backup),
  gunicorn config, nginx sample, `DEPLOY.md`. Verify a CLEAN clone builds.

## Build phases (orchestrated; each phase = one Sonnet agent, reviewed by Opus)

1. **Scaffold**: repo layout, settings split, deps, `config/`, `tenants` app with
   Organization + TenantMiddleware, custom `accounts.User` + Membership, base
   templates + theming, health check, initial migration, README. Must `runserver`.
2. **Domain models**: `venues`, `events`, `orders`, all models + migrations +
   admin + a seed/`create_demo_tenant` management command. Model-level tests.
3. **Storefront**: browse/event/performance pages, GA qty + reserved seat picker,
   cart, Hold create/refresh, availability math with locking. Hold sweeper command.
4. **Payments + tickets**: Stripe checkout session, webhook → order/ticket creation,
   QR generation, ticket emails, confirmation pages. Idempotency + signature verify.
5. **Staff dashboard + scanning**: role-gated dashboard (events CRUD, orders,
   simple reports), scanner UI + redeem endpoint.
6. **Deploy (lab980-aligned)**: `bin/boxoffice` operate CLI symlinked to
   `/usr/local/bin/boxoffice` with subcommands `deploy` (git fetch + reset --hard
   origin + pip install + migrate + collectstatic + `pm2 restart boxoffice`),
   `restart`, `logs`, `migrate`, `backup` (copies `data/`), `add-tenant <sub>`
   (DNS via doctl + nginx vhost proxying to the shared PORT + per-site certbot +
   create Organization) and `remove-tenant <sub>`. Ship a gunicorn config binding
   `127.0.0.1:$PORT` (PORT from `.env`), a pm2 start line, a sample nginx vhost
   matching lab980 `provision-site` output, WhiteNoise for static, and `DEPLOY.md`
   documenting first-time provisioning (`provision-site boxoffice ivjames/boxoffice`),
   the per-tenant subdomain flow, and env vars. Reuse the lab980 provisioning/
   certbot tooling rather than reinventing DNS/TLS. Verify a CLEAN clone builds.
