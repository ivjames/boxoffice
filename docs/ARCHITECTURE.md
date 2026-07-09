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
- Payments: Stripe Checkout Sessions + webhooks. Per-tenant Stripe keys (each
  theater connects its own Stripe account) stored on the Organization.
- QR: `segno` (pure-python, no C deps). Email: Django SMTP backend.
- Deploy target: lab980 droplet, gunicorn under pm2, nginx proxy, `/var/www/boxoffice`.
  gunicorn binds `127.0.0.1:$PORT` where PORT comes from the app-dir `.env`
  (seeded by lab980 `provision-site`). Prod settings set
  `SECURE_PROXY_SSL_HEADER=('HTTP_X_FORWARDED_PROTO','https')`,
  `ALLOWED_HOSTS`/`CSRF_TRUSTED_ORIGINS` covering `.lab980.com`.

## Multi-tenancy

Shared-schema, row-level tenancy (NOT schema-per-tenant). Simpler ops, fine for MVP.

- `Organization` (a.k.a. tenant/theater): `name`, `slug`, `subdomain` (unique),
  branding (`logo`, `primary_color`, `accent_color`), `timezone`, `currency`,
  Stripe fields (`stripe_publishable_key`, `stripe_secret_key`,
  `stripe_webhook_secret` — encrypted/secret), contact email, `is_active`.
- `TenantMiddleware` resolves the Organization from the request Host header
  (subdomain). Attaches `request.organization`. Unknown/inactive subdomain → 404.
  A reserved subdomain (`www`, `app`, `admin`, none) serves the marketing/landing +
  tenant signup + platform staff area.
- **Subdomain onboarding matches the lab980 model — no wildcard.** Each tenant
  gets a real `<sub>.lab980.com` provisioned the lab980 way: one DNS A record
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

## Key data & flows

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

1. Cart → create/refresh `Hold`. 2. POST checkout → create Stripe Checkout Session
using THAT tenant's Stripe secret key, `success_url`/`cancel_url` on the tenant
subdomain, metadata = hold id. 3. Stripe webhook (`checkout.session.completed`)
→ verify signature with tenant `stripe_webhook_secret` → within a transaction:
re-validate the hold, create `Order` + `Ticket`s (each with a signed QR token),
mark seats/GA sold, delete the hold → email tickets. Idempotent on session id.

### Ticket & scanning

- `Ticket`: `order`, `performance`, `seat` (nullable for GA), `holder_name`,
  `token` (UUID), `status` (`valid`|`used`|`void`), `used_at`, `scanned_by`.
- QR encodes a URL `/scan/redeem/<ticket_uuid>/?sig=<hmac>` where `sig` = HMAC of
  the uuid with the tenant/app secret. Scan view (role `scanner`+) verifies sig,
  checks status, atomically flips `valid`→`used`, returns pass/fail UI.

## URL structure (per tenant subdomain)

- `/` storefront home (upcoming events)
- `/events/<event-slug>/` event + performance list
- `/performances/<id>/` seat/qty selection
- `/cart/`, `/checkout/`, `/checkout/success/`, `/checkout/cancel/`
- `/tickets/<order-token>/` order confirmation + tickets
- `/dashboard/` staff area (events CRUD, orders, reports)
- `/scan/` scanner UI, `/scan/redeem/<uuid>/`
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
