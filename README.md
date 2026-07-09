# Boxoffice

White-label, multi-tenant theater box office SaaS. One Django deployment
serves many branded theaters, each on its own subdomain: public storefront
(browse → buy → Stripe checkout → emailed QR ticket) plus a staff dashboard
(manage events, view orders, scan tickets at the door).

See `docs/ARCHITECTURE.md` for the full spec. This repo is being built in
phases; **this is Phase 1 (scaffold)**: project layout, settings, the
`tenants` app (Organization + TenantMiddleware + branding), the `accounts`
app (custom email-login User + Membership/roles), base templates, and a
health check. No booking/payments/domain models yet — those land in later
phases.

## Stack

Python 3.11+, Django 5.x, PostgreSQL in prod (SQLite for zero-setup local
dev), server-rendered templates, Alpine.js (vendored static file, no
bundler), Stripe Checkout (later phase), `segno` for QR codes (later phase).

## Quickstart (local dev)

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt

# Optional: cp .env.example .env and edit. Dev settings work with zero env
# vars — DATABASE_URL falls back to a local db.sqlite3 file, DEBUG defaults
# to true, ALLOWED_HOSTS defaults to "*".
cp .env.example .env

python manage.py migrate
python manage.py createsuperuser   # optional, for /admin/
python manage.py runserver
```

`manage.py` defaults to `DJANGO_SETTINGS_MODULE=config.settings.dev`. Prod
deploys must set `DJANGO_SETTINGS_MODULE=config.settings.prod` explicitly
(and a real `DATABASE_URL` — prod has no SQLite fallback).

Visit `http://localhost:8000/healthz` — should return `{"status": "ok"}`.
Visit `http://localhost:8000/` — with no tenant resolved, this renders the
platform landing placeholder.

## Multi-tenancy: hitting a tenant subdomain locally

In production, the tenant is resolved from the subdomain of the `Host`
header (e.g. `roxy.lab980.com` → the `roxy` Organization), via
`tenants.middleware.TenantMiddleware`. Reserved subdomains (`www`, `app`,
`admin`, or no subdomain at all) resolve to the platform host instead of a
tenant (`request.organization = None`) — see `RESERVED_SUBDOMAINS` /
`BASE_DOMAIN` in `.env`.

Real subdomains are awkward to hit from `runserver` on `localhost` without
editing `/etc/hosts` per tenant. So **when `DEBUG=True`**, the middleware
also accepts a dev-only override — never active in production regardless of
what a client sends:

```bash
# Create a tenant first, e.g. via the shell:
python manage.py shell -c "
from tenants.models import Organization
Organization.objects.create(
    name='The Roxy Theater', slug='roxy', subdomain='roxy',
    contact_email='box@roxy.example',
)"

# Then hit it either via a query param...
curl "http://localhost:8000/?_tenant=roxy"

# ...or an X-Tenant header:
curl -H "X-Tenant: roxy" http://localhost:8000/
```

Either resolves `request.organization` to that Organization for the request,
including its branding (colors/logo show up via CSS custom properties in
`templates/base.html`). An unknown or inactive tenant subdomain 404s.

Alternatively, for something closer to production, add a `/etc/hosts` entry
(e.g. `127.0.0.1 roxy.localhost`) and set `BASE_DOMAIN=localhost` in `.env`;
`roxy.localhost:8000` will then resolve the same way via the real Host
header, no override needed.

## Repo layout

```
config/                  Django project: settings, urls, wsgi/asgi
  settings/
    base.py              Shared settings
    dev.py                DEBUG=True, SQLite fallback, console email
    prod.py               DEBUG=False, Postgres required, SMTP email
tenants/                  Organization model, TenantMiddleware, branding,
                          TenantScopedManager/TenantScopedModel base classes
                          for later tenant-owned apps, /healthz
accounts/                 Custom User (email login), Membership + roles
templates/
  base.html               Shared layout, per-tenant CSS variables
  tenants/                Storefront home / platform landing placeholders
static/
  css/app.css             Single committed stylesheet (no build step)
  js/alpine.min.js         Vendored Alpine.js (no CDN dependency, no bundler)
docs/ARCHITECTURE.md      Full spec (read this first)
```

## Database

- **Dev**: `DATABASE_URL` is optional. If unset, `config/settings/dev.py`
  falls back to a local `sqlite3` file (`db.sqlite3`, gitignored) — zero
  external setup for a fresh clone. Set `DATABASE_URL` to a local Postgres
  in `.env` to test Postgres-specific behavior (recommended once seat
  locking lands in Phase 3, since SQLite's locking semantics diverge from
  Postgres's `select_for_update()`).
- **Prod**: `config/settings/prod.py` requires `DATABASE_URL` — there is no
  fallback, so a misconfigured prod deploy fails fast at startup rather than
  silently writing to a local SQLite file.

## Tests

```bash
pytest
```

(No tests yet beyond what Django's `check` framework covers — Phase 1 is
scaffolding. Model/locking/tenant-isolation tests land with the domain
models in later phases.)

## Roles

`Membership(user, organization, role)` — roles are cumulative: `owner` >
`manager` > `box_office` > `scanner`. See the `can_*`/`is_*` helper methods
on `Membership` in `accounts/models.py`.
