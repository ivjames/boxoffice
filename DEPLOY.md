# Deploying boxoffice

Target: the lab980 droplet, one-dir-per-site, gunicorn under pm2 behind an
nginx proxy, per-site certbot TLS, no wildcard DNS/cert. See
`docs/ARCHITECTURE.md` (Stack + Multi-tenancy sections) for the "why"; this
doc is the concrete "how".

`DJANGO_SETTINGS_MODULE=config.settings.prod` is required for every prod
command below. `bin/boxoffice` exports it by default (from `.env`, or
hard-coded as a default if `.env` doesn't set it) so you don't normally have
to type it — it's called out explicitly here anyway because it's the single
most common way to break a deploy (accidentally running under `dev` settings,
which have no `ALLOWED_HOSTS` lockdown and a different SQLite file).

## First-time provisioning

Everything below runs **on the droplet**, as root (lab980 apps run as root —
no dedicated service user).

### 0. Prerequisites (this is the droplet's first Python site)

The lab980 box has been Node/static until now, so confirm two things first —
both have bitten a real deploy:

```bash
doctl account get          # provision-site's DNS step fails hard if doctl isn't authed
python3 --version          # need >= 3.10 for Django 5.2
apt-get install -y python3-venv python3-dev   # often absent on a Node-first box
```

If the repo is private, export a token before provisioning so the clone works:
`export GITHUB_TOKEN=<PAT with repo read>`.

### 1. Scaffold the site (DNS + dir + repo + nginx + TLS)

```bash
provision-site boxoffice ivjames/boxoffice
```

This is the lab980 `bin/provision-site` script (see `lab980.com/bin/`, and
`lab980.com/CLAUDE.md` for the platform conventions it encodes). It:

- creates the DNS A record `boxoffice.lab980.com -> <droplet IP>` (doctl)
- creates `/var/www/boxoffice` and `/var/www/boxoffice/data`
- clones this repo into `/var/www/boxoffice`
- reserves a local port (8060+) and seeds it into `/var/www/boxoffice/.env`
  as `PORT=<n>`
- writes `/etc/nginx/sites-available/boxoffice.lab980.com` (plain proxy to
  `127.0.0.1:$PORT`) and symlinks it into `sites-enabled`
- issues a certbot cert for `boxoffice.lab980.com` with the 80->443 redirect

It deliberately stops there — it does not build or start the app.

### 2. Build the app

```bash
cd /var/www/boxoffice
python3 -m venv venv
venv/bin/pip install -r requirements.txt
```

### 3. Configure `.env`

`provision-site` already wrote `PORT=<n>` into `.env`. Fill in the rest —
copy `.env.example` for the full annotated list, but at minimum:

```bash
# Generate a SHELL-SAFE secret key (alphanumeric — Django's default key can
# contain ()$#&* which are awkward in an env file) and write it single-quoted:
KEY=$(venv/bin/python -c "import secrets,string; print(''.join(secrets.choice(string.ascii_letters+string.digits) for _ in range(64)))")
printf "SECRET_KEY='%s'\n" "$KEY" >> .env

# The rest. Keep each comment on its OWN line — never inline after a value.
cat >> .env <<'EOF'
DEBUG=false
DJANGO_SETTINGS_MODULE=config.settings.prod
BASE_DOMAIN=lab980.com
RESERVED_SUBDOMAINS=www,app,admin,boxoffice
ALLOWED_HOSTS=boxoffice.lab980.com,.lab980.com
CSRF_TRUSTED_ORIGINS=https://*.lab980.com,https://lab980.com
WEB_CONCURRENCY=3
EMAIL_HOST=<your SMTP host>
EMAIL_HOST_USER=<...>
EMAIL_HOST_PASSWORD=<...>
DEFAULT_FROM_EMAIL=no-reply@lab980.com
EOF
chmod 600 .env
```

Two `.env` rules the hard way:
- **`RESERVED_SUBDOMAINS` must include `boxoffice`** (the platform's own
  subdomain). Otherwise `boxoffice.lab980.com` is parsed as a nonexistent
  tenant and 404s. Add every future platform host (e.g. `app`, `admin`) here too.
- `bin/boxoffice` reads this file literally (not via bash). It strips an
  inline `# comment` after an unquoted value, but keep comments on their own
  line anyway — and single-quote any value containing `#`, `)`, `$`, or spaces.

Leave `DATABASE_URL` unset for the default SQLite-in-`data/` deploy (see
"SQLite -> Postgres" below for the upgrade path).

### 4. Symlink the operate CLI

```bash
ln -sf /var/www/boxoffice/bin/boxoffice /usr/local/bin/boxoffice
```

From here on, run `boxoffice <command>` from anywhere — it resolves its own
app directory from the symlink target, not `cwd`.

### 5. Migrate, deploy, create an admin user

```bash
boxoffice migrate
boxoffice deploy            # pip install (no-op first time) + migrate + collectstatic + pm2 start/restart
DJANGO_SETTINGS_MODULE=config.settings.prod venv/bin/python manage.py createsuperuser
```

Note the explicit `DJANGO_SETTINGS_MODULE=config.settings.prod` on
`createsuperuser`: a bare `manage.py` defaults to **dev** settings, which would
create the admin in the dev SQLite (repo root), not the prod DB in `data/` —
and you'd never be able to log in on the live site. `boxoffice`-wrapped
commands already export prod settings from `.env`; only direct `manage.py`
calls need the prefix.

`boxoffice deploy` starts the pm2 app automatically the first time (from
`deploy/ecosystem.config.js`, name `boxoffice`, running `bin/boxoffice
serve`) and just restarts it on every subsequent deploy. If you'd rather
start it by hand once:

```bash
pm2 start deploy/ecosystem.config.js
pm2 save
```

`bin/boxoffice serve` execs:

```
venv/bin/gunicorn config.wsgi:application --bind 127.0.0.1:$PORT \
  --workers ${WEB_CONCURRENCY:-3} --access-logfile - --error-logfile -
```

### 6. Verify

```bash
curl -s https://boxoffice.lab980.com/healthz   # {"status": "ok"}
```

With no tenant Organization yet, `boxoffice.lab980.com` serves the platform
landing page — but only because `boxoffice` is listed in `RESERVED_SUBDOMAINS`
(step 3); omit it and this host 404s as a nonexistent tenant. `/admin/` is
where you'll set up tenants' Stripe keys and branding once they're onboarded
(next section).

### 7. Install the Hold sweeper

Phase 3's `release_expired_holds` command needs to run on a schedule (every
minute is fine — it's a cheap, idempotent DELETE) so seat/GA inventory frees
up promptly after a hold expires. Two equivalent options, pick one:

```bash
# cron (simplest, matches most other lab980 sites):
(crontab -l 2>/dev/null; grep -v '^#' deploy/boxoffice-sweeper.cron) | crontab -

# systemd timer, if you'd rather not touch root's crontab:
cp deploy/systemd/boxoffice-sweeper.{service,timer} /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now boxoffice-sweeper.timer
```

## Onboarding a tenant (no-wildcard subdomain flow)

Every theater gets a real `<sub>.lab980.com` — no wildcard DNS or cert. All
tenant subdomains proxy to the SAME boxoffice gunicorn process/port;
`TenantMiddleware` resolves which Organization a request belongs to from the
`Host` header. Onboarding is one command:

```bash
boxoffice add-tenant roxy --name "The Roxy Theater"
```

This does, in order:

1. **DB**: `manage.py provision_tenant roxy --name "The Roxy Theater"` —
   creates the `Organization` row (idempotent; safe to re-run). This step
   does **not** require root.
2. **DNS**: creates the `roxy.lab980.com -> <droplet IP>` A record via
   `doctl` (idempotent — leaves an existing record alone). Requires root.
3. **nginx**: writes `/etc/nginx/sites-available/roxy.lab980.com` — the
   exact proxy-to-port block from `deploy/nginx.sample.conf` /
   lab980 `provision-site`, pointed at the SAME `$PORT` as the main app —
   and symlinks + reloads. Requires root.
4. **TLS**: `certbot --nginx -d roxy.lab980.com --redirect -n`. Requires
   root.

If you run `add-tenant` **not** as root, step 1 still runs (so the org
exists) and the command stops with a clear message telling you to re-run as
root with `--infra-only` to finish DNS/nginx/TLS. Use `--db-only` to
deliberately skip infra (e.g. staging the Organization before DNS
propagates, or in an environment with no droplet/doctl at all — this is
exactly what local verification of this tooling used, since there's no real
droplet here).

**Then, finish onboarding in `/admin`:** a freshly provisioned Organization
has no Stripe keys and default (placeholder) branding — the storefront works
but checkout will fail until you set:

- `stripe_publishable_key`, `stripe_secret_key`, `stripe_webhook_secret`
  (each theater connects its own Stripe account — see
  `docs/ARCHITECTURE.md` "Checkout")
- `logo`, `primary_color`, `accent_color` (branding)
- `contact_email`, `timezone`, `currency` if the `provision_tenant`
  defaults aren't right

### Removing a tenant

```bash
boxoffice remove-tenant roxy            # deactivates (is_active=False); data kept
boxoffice remove-tenant roxy --purge    # ALSO deletes the Organization + all its data (irreversible)
```

Either way this removes the nginx vhost (root required) and reloads nginx.
It does **not** automatically delete the TLS cert or DNS record — it prints
the exact commands to do so by hand, e.g.:

```
certbot delete --cert-name roxy.lab980.com -n
doctl compute domain records list lab980.com --format ID,Type,Name \
  | awk -v n=roxy '$2=="A" && $3==n {print $1}' \
  | xargs -I{} doctl compute domain records delete lab980.com {} -f
```

(This mirrors lab980 `deprovision-site`'s DNS/cert removal, but boxoffice's
`remove-tenant` is deliberately more conservative — killing DNS/TLS the
moment you deactivate an org is riskier than for a whole-site teardown, since
reactivating a tenant is common and shouldn't require re-provisioning
infrastructure.)

## Updates

```bash
boxoffice deploy               # git fetch + reset --hard origin/main, pip install,
                                # migrate, collectstatic --noinput, pm2 restart
boxoffice deploy origin/some-branch   # deploy a specific ref instead of origin/main
```

`collectstatic --noinput` is **required**, not optional — the Phase 5
vendored static files (`static/js/jsQR.js`, `static/js/scanner.js`,
`static/js/alpine.min.js`, `static/css/app.css`) have to land in WhiteNoise's
manifest (`staticfiles/staticfiles.json`) or template `{% static %}` tags for
them 500 in prod (`CompressedManifestStaticFilesStorage` raises on a missing
manifest entry). `boxoffice deploy` runs it with `--clear` so stale files
from a previous deploy don't linger.

`boxoffice deploy` prints the old/new commit and a diffstat of what changed,
same idea as `lab980.com/update.sh`.

Other operate commands:

```bash
boxoffice restart      # pm2 restart boxoffice
boxoffice logs         # pm2 logs boxoffice (pass extra pm2 log flags through)
boxoffice migrate       # manage.py migrate only, no deploy
```

## Backups

```bash
boxoffice backup
```

- **SQLite (default)**: tars `data/` (the SQLite file) + `media/` (uploaded
  logos) into `backups/boxoffice-<timestamp>.tar.gz`.
- **Postgres** (`DATABASE_URL` set to a `postgres://`/`postgresql://` URL):
  runs `pg_dump` instead of copying files, into
  `backups/db-<timestamp>.sql.gz`, plus a separate `media-<timestamp>.tar.gz`
  if `media/` is non-empty.

`backups/` is not pruned automatically — wire up your own retention (e.g. a
cron line that deletes `backups/*` older than N days) if you want one.

## SQLite -> Postgres upgrade path

The default deploy uses SQLite in `data/db.sqlite3`
(`config/settings/prod.py`, `harden_sqlite()` sets `transaction_mode =
IMMEDIATE` + a busy timeout so concurrent checkouts serialize instead of
racing). This is deliberate for the lab980 one-dir-per-site model — no extra
infra, and correctness holds because every booking mutation runs inside
`transaction.atomic()` with availability re-checked, plus
`select_for_update()` in booking code (a no-op on SQLite, real row locking on
Postgres).

To upgrade:

1. Stand up a Postgres database (on the droplet or managed).
2. Set `DATABASE_URL=postgres://user:pass@host:5432/dbname` in `.env`.
3. `boxoffice backup` first (belt and suspenders).
4. `boxoffice migrate` — creates the schema in the new Postgres database.
   (This does NOT migrate existing SQLite data; use `manage.py dumpdata` /
   `loaddata`, or a tool like `pgloader`, if you need to carry rows over
   rather than starting fresh.)
5. `boxoffice restart`.

Nothing else in the app changes — `psycopg` is already in
`requirements.txt` specifically so this is a one-env-var upgrade.

## Troubleshooting

- **`boxoffice: command not found`**: the symlink at `/usr/local/bin/boxoffice`
  is missing or points at the wrong path — re-run step 4 of first-time
  provisioning.
- **500s after a deploy, static files 404 or raise `ValueError: Missing
  staticfiles manifest entry`**: `collectstatic` didn't run or didn't
  complete — re-run `boxoffice deploy` (or `venv/bin/python manage.py
  collectstatic --noinput --clear` directly) and check for errors.
- **`no such table` errors**: migrations haven't run against the file
  `config/settings/prod.py` actually points at (`data/db.sqlite3`, not the
  dev-only `db.sqlite3` at the repo root) — run `boxoffice migrate` with
  `DJANGO_SETTINGS_MODULE=config.settings.prod` set.
- **A tenant subdomain 404s**: either the `Organization.is_active` is
  `False`, the subdomain doesn't match any Organization, or the nginx vhost
  for it doesn't exist/didn't reload — check `boxoffice add-tenant` ran to
  completion (as root) and `nginx -t`.
