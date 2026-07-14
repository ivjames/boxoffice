# Patron-revenue roadmap (closing the Ludus gaps)

Where this comes from: a competitive read against Ludus. Our core ticketing engine
(seat locking, tenant isolation, QR security, Stripe Connect direct charges) is
solid — arguably more carefully engineered than the competition's. What loses a
competitive theater sale is breadth of the **patron-revenue suite**: discounts,
passes, donations, and turning ticket buyers into a mailing list. This document is
the build order for closing those gaps. All five areas are greenfield today (no
models, stubs, or TODOs exist for any of them).

Sequencing is dependency-optimized rather than strictly business-priority ordered:
donations and passes both need a "non-ticket line item" concept (`Order.performance`
is currently a non-nullable FK; `OrderItem` has no kind discriminator), so that
foundation is built once, in Phase 2, before both features that need it.

Each phase ships as its own PR to `staging` (beta-first — see DEPLOY.md), verified
on beta.boxo.show before promotion to `main`.

---

## Phase 1 — Discounts / promo codes  *(in progress)*

Table-stakes; the cheapest high-value add. One new `promotions` app plus wiring
into the existing money path.

- `PromoCode` (tenant-scoped): percent or fixed-amount, active window, max
  redemptions, min order, org-wide in v1 (per-event scoping is one nullable FK +
  one clause in `validate_code_for_hold` later — all usability checks route
  through that single predicate).
- Discount **snapshots onto the Hold** at apply time (`discount_amount`,
  `promo_code_text`), mirroring the price-snapshot pattern — a later code edit
  can't change what a buyer is charged.
- `hold_total()` stays the gross subtotal; new `hold_grand_total()` is the net.
  `Order.total` records the **net** charged amount (so refunds are automatically
  correct); `discount_amount`/`promo_code_text` ride on the Order for reporting.
- Stripe: ad-hoc `duration="once"` Coupon created on the connected account,
  passed via the Checkout Session's `discounts` — the hosted page shows the
  discount as its own line and the charge reconciles exactly with `Order.total`.
  Platform application fee is computed on the net.
- Redemption cap is soft-enforced at apply time (lock-then-recheck) and counted
  at fulfillment; a paid order is **never** rejected for promo state.
- Buyer UX: code input per cart item (holds are per-performance; checkout is
  per-hold). Staff UX: manager-tier CRUD under `dashboard/promos/`.

## Phase 2 — Order foundation + Donations

Nonprofits live on donations; often a checkout add-on ("add a $10 donation").

1. **Foundation refactor** (the one cross-cutting schema change; enables
   donations AND passes):
   - `Order.performance` becomes nullable; `OrderItem` gains a `kind`
     discriminator (`ticket | donation | pass`). v1 keeps `Order.performance`
     for pure-ticket orders; null for donation-only/pass orders.
   - `_line_items_for_hold` and `fulfill_hold` generalize to iterate item kinds.
2. **Donations**:
   - `donations` app: `DonationCampaign` (name, blurb, suggested amounts,
     active); v1 may start as a single implicit "general fund" per org.
   - Checkout add-on: preset buttons + custom amount on the cart → extra Stripe
     line item → `OrderItem(kind=donation)`.
   - Standalone `/donate/` page for donation-only orders (no hold needed).
   - Dashboard: donation totals report, order-detail line, CSV export.
   - Receipt email carries per-org nonprofit acknowledgment text.

## Phase 3 — Season & flex passes (one-time purchase, entitlement model)

The #1 reason serious theaters pick a "real" platform; drives renewal revenue.
Decision: passes are **one-time purchases** (mode=payment), not recurring Stripe
subscriptions — reuses the whole existing checkout path and avoids pre-created
Price/Product objects.

- `passes` app: `PassProduct` (name, price, kind=season|flex, credit_count for
  flex, valid events/window), `PassPurchase` (linked to GuestAccount + Order),
  `PassRedemption` (pass → ticket linkage, credit decrement).
- Sold through normal checkout as `OrderItem(kind=pass)` (Phase 2 foundation).
- Redemption: guest portal "use my pass" → pick performance/seats → $0
  credit-decrement checkout → normal Ticket minting via a fulfill path that
  consumes entitlements instead of charging. Credit races guarded with the
  existing select_for_update/IMMEDIATE pattern.
- Dashboard: pass product CRUD; sales & outstanding-liability report.

## Phase 4 — CRM + email marketing

Turn ticket buyers into a mailing list. `GuestAccount` (per-org, email-keyed,
already linked to every order at fulfillment) is the anchor.

- Extend `GuestAccount`: `marketing_opt_in` + consent timestamp, tags, notes.
  LTV/order-count stay computed queries, not stored columns.
- Consent first: opt-in capture at checkout and in the guest portal; signed
  one-click unsubscribe link — a legal prerequisite for any bulk send.
- `campaigns` app: `EmailCampaign` (subject, body, segment, status) +
  `CampaignSend` per-recipient log. v1 segments: all opted-in, by event
  purchased, by min spend.
- Sending: management-command batch sender on cron (same shape as the hold
  sweeper) — no Celery. SMTP first; pluggable transactional provider later.
- Dashboard: audience list with purchase history, campaign composer + preview,
  sent/failed counts.

## Phase 5 — Support posture (docs & self-serve, the anti-Ludus trade)

We won't match Ludus's human support as a lean product, so lean the opposite
way: great self-serve, and price as the trade.

- Expand `helpcenter`: staff onboarding guides, per-tenant patron FAQ.
- In-dashboard contextual help links; "getting started" checklist on overview.
- Keep the platform fee at/near zero as the explicit positioning trade.
- Mostly content + small UI; schedule opportunistically between phases.
