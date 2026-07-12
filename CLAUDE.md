# boxoffice — working conventions for agents

Django white-label box office for independent theaters (boxo.show). Full
deploy/runbook is in `DEPLOY.md`; architecture in `docs/ARCHITECTURE.md`.

## Branching & PRs (beta-first — read this before opening a PR)

This repo ships **beta-first** (see `DEPLOY.md` → "Beta / staging site" and its
Promotion flow). Work lands on `staging` FIRST — `staging` auto-deploys to
`beta.boxo.show` — and is later merged `staging → main`, which deploys to prod.

- Cut feature branches from `staging`, and open PRs **against `staging`** —
  never straight to `main`. `main` only advances via a `staging → main`
  promotion once the beta looks good; opening a feature PR against `main` is
  the wrong base.
- **This is an autonomous setup: there is no human reviewer to wait on.** Open
  the PR and merge it yourself — don't block on a review, an approval, or a
  green check that isn't coming. There is currently no CI configured, so
  nothing runs on the PR; verify your change locally (tests + drive the real
  app) before merging, since the PR gate won't catch it for you.

## Frontend / styling

`static/css/app.css` is shared across every tenant storefront AND the staff
dashboard, keyed on per-tenant `--primary-color` / `--accent-color` (set inline
in `templates/base.html` from `request.organization`). Keep it tenant-agnostic —
don't hardcode brand colors there.

The boxo.show **marketing landing page** (platform host, `organization is None`
— `templates/tenants/platform_landing.html`) has its own theatrical design
system, scoped under the `.platform-landing` body class
(`static/css/boxo-tokens.css` + `boxo-landing.css`, loaded only on that page).
Keep landing-only styling scoped there so it never bleeds into tenant branding
or the dashboard.

## Verifying

`STORAGES` uses WhiteNoise's manifest storage even in dev, so run
`python manage.py collectstatic --noinput` before `runserver`/tests or static
lookups raise "Missing staticfiles manifest entry". The seating-editor test
suite is slow; scope test runs to the app you touched (e.g.
`python manage.py test orders.test_views tenants`) rather than the full suite.
