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

The platform host is the **bare apex** `boxo.show` (not a subdomain), so
scaffold it with `provision-site`'s apex mode (`@`):

```bash
provision-site @ ivjames/boxoffice --domain boxo.show --dir /var/www/boxoffice
```

This is the lab980 `bin/provision-site` script (see `lab980.com/bin/`, and
`lab980.com/CLAUDE.md` for the platform conventions it encodes). In apex mode it:

- creates the DNS A records `boxo.show -> <droplet IP>` and
  `www.boxo.show -> <droplet IP>` (doctl)
- creates `/var/www/boxoffice` and `/var/www/boxoffice/data`
- clones this repo into `/var/www/boxoffice`
- reserves a local port (8060+) and seeds it into `/var/www/boxoffice/.env`
  as `PORT=<n>`
- writes `/etc/nginx/sites-available/boxo.show` (plain proxy to
  `127.0.0.1:$PORT`, `server_name boxo.show www.boxo.show`) and symlinks it
  into `sites-enabled`
- issues one certbot cert covering `boxo.show` + `www.boxo.show` with the
  80->443 redirect

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
BASE_DOMAIN=boxo.show
RESERVED_SUBDOMAINS=www,app,admin,beta
ALLOWED_HOSTS=boxo.show,.boxo.show
CSRF_TRUSTED_ORIGINS=https://*.boxo.show,https://boxo.show
WEB_CONCURRENCY=3
EMAIL_HOST=<your SMTP host>
EMAIL_HOST_USER=<...>
EMAIL_HOST_PASSWORD=<...>
DEFAULT_FROM_EMAIL=no-reply@boxo.show
EOF
chmod 600 .env
```

Two `.env` rules the hard way:
- **The platform host is the bare apex `boxo.show`**, so it needs no reserved
  subdomain of its own (a bare/absent Host subdomain always resolves to the
  platform host). Do keep `beta` in `RESERVED_SUBDOMAINS` — `beta.boxo.show` is
  the staging deploy's host (see "Beta / staging site" below) — plus any other
  platform hosts (`www`, `app`, `admin`) so a tenant can never claim them.
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
boxoffice manage createsuperuser
```

Run one-off management commands through `boxoffice manage <cmd>`, not a bare
`venv/bin/python manage.py <cmd>`: `manage.py` defaults to **dev** settings,
which would create the admin in the dev SQLite (repo root), not the prod DB in
`data/` — and you'd never be able to log in on the live site. The `boxoffice`
operate CLI sources `.env` and exports prod settings for every subcommand,
including `manage`. (If you must call `manage.py` directly, prefix it:
`DJANGO_SETTINGS_MODULE=config.settings.prod venv/bin/python manage.py …`.)

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
curl -s https://boxo.show/healthz   # {"status": "ok"}
```

With no tenant Organization yet, `boxo.show` (the bare apex) serves the
platform landing page — the platform-host path in `TenantMiddleware`, which a
bare/absent subdomain always takes, so there's no reserved-subdomain caveat
here anymore. `/admin/` is where you'll set up tenants' branding once they're
onboarded; each theater connects its own payouts via Stripe Connect from its
dashboard (next section).

**Tenants live on their own subdomains.** The platform host (bare `boxo.show`
/ any reserved subdomain) always serves the marketing landing page and
`/admin/` — it never serves a theater's catalog. Each venue gets its own
`<sub>.boxo.show` via `boxoffice add-tenant` (next section), and `/`,
`/login`, `/dashboard`, `/scan` all work against that subdomain.

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

### 8. Install the campaign email sender

Phase 4 (CRM + email marketing) sends campaign emails from a batch worker on a
schedule — the exact same cron/systemd shape as the Hold sweeper above, just a
different command. When a staffer triggers a campaign from the dashboard,
`campaigns.services.start_campaign` fans it out into `pending` `CampaignSend`
rows and marks the campaign `sending`; this worker
(`manage.py send_campaign_emails`) drains those rows, sending one email each.

It's safe to run often: a no-op when nothing is queued, each row is **claimed
atomically** (a conditional `pending -> sending` UPDATE) so overlapping ticks
can't double-send, opt-in is **re-checked at send time** (an unsubscribe
between trigger and send is honored as `skipped`, never mailed), and each run
is capped at `CAMPAIGN_BATCH_SIZE` rows (default 50) so a large blast paces out
across ticks instead of blocking one run on thousands of SMTP round-trips. When
a campaign's last row drains, the worker flips it to `sent`.

Unlike the tenant provisioner it does **not** need root (no certbot/nginx/doctl
— just DB + SMTP), but it does need `DJANGO_SETTINGS_MODULE=config.settings.prod`
(baked into both deploy files) so it reads the prod DB the dashboard writes to.
Two equivalent options, pick one:

```bash
# cron (every 2 minutes — comfortable for a rate-limited SMTP relay; drop to
# every minute on a fast transactional provider):
(crontab -l 2>/dev/null; grep -v '^#' deploy/boxoffice-campaigns.cron) | crontab -

# systemd timer:
cp deploy/systemd/boxoffice-campaigns.{service,timer} /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now boxoffice-campaigns.timer
```

**Deliverability gate.** The worker won't send until email delivery is actually
configured (`guests.services.email_delivery_configured()` — the same check the
guest sign-in portal uses): if the prod SMTP backend is selected but
`EMAIL_HOST` is still blank, it leaves the sends `pending` and logs a note
rather than burning them against a dead transport. Wire up SMTP (`.env`'s
`EMAIL_*`) and the next tick picks the queue back up. `List-Unsubscribe` /
`List-Unsubscribe-Post` one-click headers are attached to every send (an
RFC 8058 bulk-sender requirement), pointing at the same signed unsubscribe link
carried in the email body. Tune throughput with `CAMPAIGN_BATCH_SIZE` in `.env`.

## Onboarding a tenant (no-wildcard subdomain flow)

Every theater gets a real `<sub>.boxo.show` — no wildcard DNS or cert. All
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
2. **DNS**: creates the `roxy.boxo.show -> <droplet IP>` A record via
   `doctl` (idempotent — leaves an existing record alone). Requires root.
3. **nginx**: writes `/etc/nginx/sites-available/roxy.boxo.show` — the
   exact proxy-to-port block from `deploy/nginx.sample.conf` /
   lab980 `provision-site`, pointed at the SAME `$PORT` as the main app —
   and symlinks + reloads. Requires root.
4. **TLS**: `certbot --nginx -d roxy.boxo.show --redirect -n`. Requires
   root.

If you run `add-tenant` **not** as root, step 1 still runs (so the org
exists) and the command stops with a clear message telling you to re-run as
root with `--infra-only` to finish DNS/nginx/TLS. Use `--db-only` to
deliberately skip infra (e.g. staging the Organization before DNS
propagates, or in an environment with no droplet/doctl at all — this is
exactly what local verification of this tooling used, since there's no real
droplet here).

**Then, finish onboarding:** a freshly provisioned Organization has default
(placeholder) branding and no connected Stripe account — the storefront works
and checkout runs in **simulated stub mode** (no real charge) until:

- **Branding** (`/admin`): `logo`, `primary_color`, `accent_color`;
  `contact_email`, `timezone`, `currency` if the `provision_tenant` defaults
  aren't right.
- **Payments** (theater dashboard, owner-only): the theater's owner clicks
  **Connect Stripe** on the dashboard Overview and completes Stripe's hosted
  Express onboarding. Once Stripe reports the account `charges_enabled`, real
  checkout switches on automatically (`stripe_charges_enabled` is cached from
  the `account.updated` webhook). No keys are ever pasted into `/admin` — the
  platform holds one set of Stripe keys in its env; the theater only ever holds
  a connected account. See `docs/ARCHITECTURE.md` "Payments".

The platform's own Stripe keys (`STRIPE_SECRET_KEY`, `STRIPE_PUBLISHABLE_KEY`,
`STRIPE_WEBHOOK_SECRET`) and take-rate (`PLATFORM_FEE_PERCENT`,
`PLATFORM_FEE_FIXED_CENTS`, default 0) are set once in the app-dir `.env` — see
`.env.example`. Register the single Connect webhook endpoint at
`https://boxo.show/webhooks/stripe/` (events: `checkout.session.completed`,
`account.updated`).

### Onboarding from the admin (no SSH)

The CLI above is the one-shot path. You can also do the whole thing from
`/admin` without touching a shell — create the `Organization` (name +
subdomain + branding), then select it and run the **"Provision
infrastructure (DNS + nginx + TLS)"** action. That's the DB half (the row you
just created) plus a *queued* infra half. (Payments aren't set here — the
theater's owner connects Stripe from the dashboard afterward.)

The split matters: the admin action only flips the tenant's `infra_status` to
**Queued** — a plain DB write. It deliberately does **not** run
certbot/nginx/doctl from the web process (that would block a gunicorn worker
for the length of a cert issuance, and hand the web tier root powers it
shouldn't have). Instead a root cron worker,
`manage.py provision_pending_tenants`, picks up queued tenants once a minute
and runs the same idempotent `add-tenant --infra-only` flow, writing the
result back to the row. Watch the **Infra status** column go
`Queued → Provisioning… → Live` (or **Failed**, with the certbot/nginx output
in `infra_message`; re-running the action retries it).

Install the worker once, in **root's** crontab (it needs root for
certbot/nginx/doctl, and prod settings to read the right DB):

```bash
(crontab -l 2>/dev/null; grep -v '^#' deploy/boxoffice-provision.cron) | crontab -
```

Without that cron entry, queued tenants just sit at **Queued** — the admin
action has nothing to run it. (It's a no-op every minute when nothing is
queued, same shape as the Hold sweeper in step 7.)

### Removing a tenant

```bash
boxoffice remove-tenant roxy            # deactivates (is_active=False); data kept
boxoffice remove-tenant roxy --purge    # ALSO deletes the Organization + all its data (irreversible)
```

Either way this removes the nginx vhost (root required) and reloads nginx.
It does **not** automatically delete the TLS cert or DNS record — it prints
the exact commands to do so by hand, e.g.:

```
certbot delete --cert-name roxy.boxo.show -n
doctl compute domain records list boxo.show --format ID,Type,Name \
  | awk -v n=roxy '$2=="A" && $3==n {print $1}' \
  | xargs -I{} doctl compute domain records delete boxo.show {} -f
```

(This mirrors lab980 `deprovision-site`'s DNS/cert removal, but boxoffice's
`remove-tenant` is deliberately more conservative — killing DNS/TLS the
moment you deactivate an org is riskier than for a whole-site teardown, since
reactivating a tenant is common and shouldn't require re-provisioning
infrastructure.)

## Beta / staging site (beta.boxo.show)

A second, isolated instance for testing releases before they hit prod: its
own app dir, port, SQLite DB, and git branch. It runs on the same droplet as
prod but shares nothing with it. This is possible with no code changes because
`bin/boxoffice` and `deploy/ecosystem.config.js` derive the pm2 app name from
the install directory, so a `/var/www/boxoffice-beta` install supervises the
pm2 app `boxoffice-beta` without colliding with prod's `boxoffice`.

Prereq: a `staging` branch exists in the repo (that's what this box tracks).
Then, on the droplet as root, top to bottom:

```bash
# 1. Scaffold beta.boxo.show on its OWN dir + port + cert. Ordinary subdomain
#    provisioning (DNS A beta.boxo.show, its own nginx vhost on a fresh 8060+
#    port, its own cert). `beta` is reserved in prod's RESERVED_SUBDOMAINS so
#    no tenant can ever take it.
provision-site beta ivjames/boxoffice --domain boxo.show --dir /var/www/boxoffice-beta

# 2. Build its venv and its OWN .env (separate SECRET_KEY + separate data/ DB
#    — never point it at prod's). provision-site already seeded PORT.
cd /var/www/boxoffice-beta
python3 -m venv venv && venv/bin/pip install -r requirements.txt
KEY=$(venv/bin/python -c "import secrets,string; print(''.join(secrets.choice(string.ascii_letters+string.digits) for _ in range(64)))")
printf "SECRET_KEY='%s'\n" "$KEY" >> .env
cat >> .env <<'EOF'
DEBUG=false
DJANGO_SETTINGS_MODULE=config.settings.prod
BASE_DOMAIN=boxo.show
# Whole-domain wildcard (leading dot) so every tenant subdomain provisioned
# on beta -- roxy.boxo.show, etc. -- is accepted, not just beta.boxo.show.
# Narrowing these to a single host makes every provisioned tenant 400
# (DisallowedHost) / 403 (CSRF) until it's hand-added. Matches prod's defaults.
ALLOWED_HOSTS=.boxo.show
CSRF_TRUSTED_ORIGINS=https://*.boxo.show
RESERVED_SUBDOMAINS=www,app,admin,beta
DEPLOY_REF=origin/staging
DEFAULT_FROM_EMAIL=no-reply@boxo.show
ENABLE_TEST_CHECKOUT=true
SHOW_ADMIN_LINK=true
EOF
chmod 600 .env

# 3. Give it its own operate symlink, migrate, deploy. It comes up as the pm2
#    app "boxoffice-beta" (derived from the dir name) tracking origin/staging.
ln -sf /var/www/boxoffice-beta/bin/boxoffice /usr/local/bin/boxoffice-beta
boxoffice-beta migrate
boxoffice-beta deploy

# 4. Verify.
curl -s https://beta.boxo.show/healthz   # {"status": "ok"}
```

Two things that bite:
- **`beta` MUST be in this box's `RESERVED_SUBDOMAINS`** (it is, above). Its
  host is `beta.boxo.show` with `BASE_DOMAIN=boxo.show`, so `beta` parses as a
  subdomain — un-reserved, `TenantMiddleware` looks for a nonexistent `beta`
  tenant and 404s. `beta.boxo.show` itself only ever serves the landing page +
  `/admin/`. To exercise a full *tenant storefront* on staging, onboard a
  throwaway tenant that lives only on the beta box —
  `boxoffice-beta add-tenant demo` gives `demo.boxo.show` its own vhost pointed
  at the beta port; seed it with `boxoffice-beta manage create_demo_tenant`
  (or `boxoffice-beta manage seed_showcase` for a whole populated platform)
  and, with `ENABLE_TEST_CHECKOUT=true`, run the whole browse→checkout→scan
  flow. With `ALLOWED_HOSTS=.boxo.show` above, its host is already accepted --
  no per-tenant edit needed. (If you deliberately pinned `ALLOWED_HOSTS`/
  `CSRF_TRUSTED_ORIGINS` to specific hosts instead, add this one to both.)
- **`DEPLOY_REF=origin/staging`** makes a bare `boxoffice-beta deploy` track
  the `staging` branch (prod tracks `origin/main`). `ENABLE_TEST_CHECKOUT=true`
  is safe here (throwaway data) and exercises checkout without Stripe — never
  set it on prod.

pm2 now supervises `boxoffice` (prod, `origin/main`) and `boxoffice-beta`
(staging, `origin/staging`) side by side. **Promotion flow:** land work on
`staging` → it auto-deploys to beta on the next `boxoffice-beta deploy` →
once it looks good, merge `staging` → `main` and `boxoffice deploy` on prod.

## Moving boxoffice to its own boxo.show domain

Boxoffice started on the `boxoffice.lab980.com` subdomain; it now moves to its
own apex domain `boxo.show` so it can carry tenant subdomains and a beta site
under a name of its own. Same droplet — a domain swap, not a server move.

There's **no live data to preserve on lab980.com** here: no already-sold tickets
whose emailed links point at the old host, and no tenant with a Stripe webhook
configured against a `*.lab980.com` URL. So this is just a clean cutover — flip
config, stand up the new vhosts, tear down the old ones. (If that ever stops
being true — you onboard a tenant and wire its Stripe webhook before cutting
over — repoint that webhook to the `boxo.show` host as part of step 4, or
checkout fulfillment stops.)

Runbook (on the droplet, as root):

```bash
# 1. Add boxo.show as a DigitalOcean DNS zone (doctl must be authed).
doctl compute domain create boxo.show

# 2. Flip the app's config to boxo.show, then redeploy so ALLOWED_HOSTS,
#    BASE_DOMAIN, CSRF and the from-address all move together (this also runs
#    the 0002 help_text migration). Edit /var/www/boxoffice/.env:
#      BASE_DOMAIN=boxo.show
#      ALLOWED_HOSTS=boxo.show,.boxo.show
#      CSRF_TRUSTED_ORIGINS=https://*.boxo.show,https://boxo.show
#      RESERVED_SUBDOMAINS=www,app,admin,beta
#      DEFAULT_FROM_EMAIL=no-reply@boxo.show
boxoffice deploy

# 3. Provision the apex platform host (DNS boxo.show + www, nginx vhost, cert).
#    /var/www/boxoffice already exists, so this only adds DNS/nginx/TLS — it
#    won't re-clone or touch .env, and it reuses the PORT already in that .env
#    so the apex vhost proxies to the running gunicorn (pass --port to override).
provision-site @ ivjames/boxoffice --domain boxo.show --dir /var/www/boxoffice

# 4. Onboard tenants on the new domain. add-tenant reads the new BASE_DOMAIN
#    from step 2, so it builds <sub>.boxo.show DNS/nginx/cert. (Its DB step is
#    idempotent, so it's also the way to re-home any tenant you'd already
#    created on lab980 — same Organization row, new boxo.show infra.)
boxoffice add-tenant roxy          # ...once per tenant subdomain
```

```text
# 5. Verify:  curl -s https://boxo.show/healthz   # {"status": "ok"}
#    plus a browse -> (test) checkout smoke test on a tenant subdomain.

# 6. Decommission the old lab980 host(s):
#      deprovision-site boxoffice            # boxoffice.lab980.com nginx + cert + DNS
#    plus any tenant vhost you'd stood up on *.lab980.com:
#      certbot delete --cert-name <sub>.lab980.com -n
#      rm -f /etc/nginx/sites-{available,enabled}/<sub>.lab980.com && systemctl reload nginx
#      doctl compute domain records list lab980.com --format ID,Type,Name \
#        | awk -v n=<sub> '$2=="A" && $3==n {print $1}' \
#        | xargs -I{} doctl compute domain records delete lab980.com {} -f
```

Moving to a **new droplet** later is an orthogonal change: rsync
`/var/www/boxoffice` (app dir carries `.env` + `data/` + `media/`) to the new
box, re-point the boxo.show DNS A records at its IP, re-issue certs with
certbot, `pm2 start deploy/ecosystem.config.js && pm2 save`. Nothing in the app
changes — it's purely infra.

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
from a previous deploy don't linger, and `--verbosity 0` so the per-file
copy/delete listing (hundreds of lines) stays out of the deploy log —
errors still surface.

`boxoffice deploy` prints the old/new commit and a diffstat of what changed,
same idea as `lab980.com/update.sh`.

**Static AND media are both served by the app** — no nginx `location` blocks.
The `WhiteNoiseMiddleware` serves hashed static files from `STATIC_ROOT`, and
`config/wsgi.py` wraps the WSGI app in a second WhiteNoise instance that
serves user-uploaded **media** (`MEDIA_ROOT`, e.g. tenant logos) at
`MEDIA_URL`. That's why every per-tenant vhost stays a plain proxy-to-port
(`deploy/nginx.sample.conf`) and needs no `location /media/` alias. The media
wrapper runs with `autorefresh=True`, so a logo uploaded via `/admin` after
the worker started is served immediately without a `boxoffice restart`
(media is mutable, unlike the immutable hashed static files). Uploaded media
lives under the app dir's `media/` and is included in `boxoffice backup`.

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
  complete — re-run `boxoffice deploy` (or `boxoffice manage collectstatic
  --noinput --clear` directly, dropping `--verbosity 0` to watch each file)
  and check for errors. Use `boxoffice manage`, not a bare `venv/bin/python
  manage.py`, so it runs under prod settings (the right STATIC_ROOT and
  manifest storage).
- **`no such table` errors**: migrations haven't run against the file
  `config/settings/prod.py` actually points at (`data/db.sqlite3`, not the
  dev-only `db.sqlite3` at the repo root) — run `boxoffice migrate` with
  `DJANGO_SETTINGS_MODULE=config.settings.prod` set.
- **A tenant subdomain 404s**: either the `Organization.is_active` is
  `False`, the subdomain doesn't match any Organization, or the nginx vhost
  for it doesn't exist/didn't reload — check `boxoffice add-tenant` ran to
  completion (as root) and `nginx -t`.
