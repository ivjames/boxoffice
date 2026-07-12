# Help center

Each theater gets a **help section** that serves two audiences from one body of
content:

- **Staff** read role-appropriate help inside the dashboard, and managers author
  their venue's own articles.
- **Ticket buyers** read the public subset as a storefront FAQ.

Lives in the `helpcenter` app. Content is a mix of **tenant-authored articles**
(`HelpArticle`, in the DB) and a shipped set of **built-in articles**
(`helpcenter/builtins.py`, read-only) that act as a fallback so the section is
useful the moment a tenant is created.

## Visibility model

Every article carries a `visibility` that decides who may read it. The tiers map
onto the `accounts` role hierarchy and are **cumulative in the same direction
roles are** (`owner > manager > box_office > scanner`):

| `visibility`  | Readable by                                   | On storefront FAQ? |
| ------------- | --------------------------------------------- | ------------------ |
| `public`      | Everyone, including anonymous ticket buyers   | ✅ yes             |
| `staff`       | Any staff role (scanner → owner)              | no                 |
| `box_office`  | box_office, manager, owner                    | no                 |
| `manager`     | manager, owner                                | no                 |
| `owner`       | owner only                                    | no                 |

So a `box_office` article is readable by box office, managers and owners; a
`staff` article by every staff role; a `public` article by everyone.

Filtering happens in exactly two places — do not re-encode the ordering
elsewhere:

- `HelpArticle.objects.readable_by(organization, membership)` — published
  articles visible to a staff member's role (dashboard).
- `HelpArticle.objects.public(organization)` — published, `public`-visibility
  articles (storefront FAQ).

Both build on `helpcenter.models.visibilities_readable_by(membership)`, which
uses `Membership`'s own cumulative role helpers (`is_box_office_or_above()`
etc.) rather than spelling the ordering out again.

## Model — `HelpArticle`

Tenant-scoped (`TenantScopedModel`). Notable fields:

- `title`, `slug` — slug auto-derives from the title if blank and is unique
  **per organization** (`unique_help_slug_per_org`); two theaters can both have
  a `policies` slug.
- `summary` — one-line teaser shown under the title in the list.
- `body` — **plain text**. Blank lines start a new paragraph; rendered with
  Django's `linebreaks` (autoescaped — no raw HTML, so no XSS surface). No
  Markdown.
- `category` — `general` | `venue_rules` | `show_info` | `policies` | `how_to`.
  Used to group articles under headings.
- `visibility` — see the table above. Default `staff`.
- `is_published` — unpublished drafts appear only on the Manage screen, never
  to readers.
- `position` — lower sorts first within a category.
- `created_by` — the author (nullable).

## Built-in fallback — `helpcenter/builtins.py`

A short list of `BuiltinArticle` dataclasses shipped with the platform (welcome,
selling/refunds, working the door, events/performances, team/roles, buyer FAQ,
venue rules). They:

- mirror the attribute surface the templates read off a `HelpArticle`
  (`title`/`slug`/`summary`/`body`/`category`/`visibility` + `get_*_display`),
  so one partial (`templates/helpcenter/_article.html`) renders both;
- carry `is_builtin = True`, so the UI shows them **without** edit/delete
  controls;
- are filtered by the same visibility rules
  (`builtins.readable_by(membership)`, `builtins.public()`) and merged into the
  same category groups as authored content.

To add or change default content, edit `BUILTIN_ARTICLES`. They are not database
rows — nothing to migrate.

## Surfaces & routes

**Staff (dashboard)** — gated by `accounts.permissions`, exactly like the rest
of the dashboard:

| Route                          | View          | Gate                    |
| ------------------------------ | ------------- | ----------------------- |
| `/dashboard/help/`             | `help_index`  | any staff (`tenant_staff_required`) |
| `/dashboard/help/manage/`      | `help_manage` | manager+ (`manager_required`) |
| `/dashboard/help/new/`         | `help_create` | manager+                |
| `/dashboard/help/<id>/edit/`   | `help_update` | manager+                |
| `/dashboard/help/<id>/delete/` | `help_delete` | manager+ (POST)         |

Every staffer sees the role-filtered **read** view; managers/owners additionally
get authoring CRUD. All writes set `organization` from `request.organization`
(never a POST field) and look articles up scoped to it, so a manager can't reach
another tenant's content (foreign id → 404). "Help" is in the dashboard nav for
all staff.

**Buyers (storefront)**:

| Route   | View         | Gate                            |
| ------- | ------------ | ------------------------------- |
| `/faq/` | `public_faq` | tenant host only (`require_tenant`) |

Shows `public` published articles + public built-ins + the box-office contact
email. 404s on the platform host. Linked from the storefront nav ("Help") and
footer.

## Styling

Help styling lives in the shared `static/css/app.css` under the `Help center`
block and is tenant-agnostic (neutral tokens + `--accent-color` for the
disclosure marker), so it inherits each theater's branding. Articles render as
native `<details>`/`<summary>` accordions — no JS.

## Tests

`helpcenter/tests.py` covers the model slug/visibility helpers, per-role staff
reading (drafts excluded), manager-only authoring with cross-org isolation, the
built-in filtering, and the public FAQ (public-published-only, 404 off a tenant
host). Run with `python manage.py test helpcenter` (after `collectstatic`, per
the repo's manifest-storage note in `CLAUDE.md`).
