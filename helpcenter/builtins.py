"""Built-in help articles shipped with the platform.

These are the "static fallback" half of the help center: a small set of
role-appropriate defaults so the Help section (and the storefront FAQ) is
useful the moment a tenant is created, before any manager has authored a
single article. They are read-only — rendered through the same templates as
tenant-authored HelpArticles but carrying `is_builtin = True`, so the UI
shows them without edit/delete controls.

Each built-in is keyed and filtered by the SAME Visibility values as the
model, reusing helpcenter.models.visibilities_readable_by so staff see the
built-ins appropriate to their role, and only PUBLIC ones reach buyers.
"""

from dataclasses import dataclass

from .models import HelpArticle, visibilities_readable_by

Visibility = HelpArticle.Visibility
Category = HelpArticle.Category


@dataclass(frozen=True)
class BuiltinArticle:
    """A shipped default article. Mirrors the attribute surface the article
    templates read off a HelpArticle (title/slug/summary/body/category/
    visibility + get_*_display) so both render through one partial."""

    slug: str
    title: str
    summary: str
    body: str
    category: str
    visibility: str
    is_builtin: bool = True

    def get_category_display(self):
        return Category(self.category).label

    def get_visibility_display(self):
        return Visibility(self.visibility).label


BUILTIN_ARTICLES = [
    BuiltinArticle(
        slug="welcome",
        title="Welcome to your box office",
        summary="What each part of the staff dashboard does.",
        category=Category.HOW_TO,
        visibility=Visibility.STAFF,
        body=(
            "Everything you need to run the door and the box office lives in the "
            "dashboard. What you can see depends on your role:\n\n"
            "• Overview — sales and upcoming performances at a glance.\n"
            "• Events & Seating — create shows and lay out the house (managers).\n"
            "• Orders — look up a buyer, resend tickets, issue a refund (box office).\n"
            "• Scan — check tickets in at the door (everyone).\n"
            "• Team — invite staff and set their roles (managers).\n"
            "• Help — this section. Your managers can add articles for your venue "
            "with house rules, show notes and policies."
        ),
    ),
    BuiltinArticle(
        slug="getting-your-box-office-set-up",
        title="Getting your box office set up",
        summary="Five steps from signup to your first sale.",
        category=Category.HOW_TO,
        visibility=Visibility.MANAGER,
        body=(
            "There are five things to do before you can sell your first ticket:\n\n"
            "1. Connect Stripe — only an owner can do this, from the setup checklist "
            "on the Overview page. Ticket money goes straight to your own Stripe "
            "account; boxo.show never holds it.\n"
            "2. Add a venue and build your seating chart, or plan to sell a "
            "performance as general admission, under Seating.\n"
            "3. Create your event under Events, add a performance for each date, "
            "and set both the event and the performance to Published when you're "
            "ready to sell.\n"
            "4. Set price tiers for each performance under Pricing.\n"
            "5. Invite your team under Team and give each person a role — Owner, "
            "Manager, Box office or Scanner.\n\n"
            "Once a performance is published with prices set, it's live on your "
            "storefront immediately — there's no separate \"go live\" step."
        ),
    ),
    BuiltinArticle(
        slug="selling-and-refunds",
        title="Selling tickets and issuing refunds",
        summary="Look up an order, resend tickets, and process a refund.",
        category=Category.HOW_TO,
        visibility=Visibility.BOX_OFFICE,
        body=(
            "Open Orders to find any purchase by buyer email or order number. From "
            "an order you can resend the confirmation email, view the tickets, and "
            "— when your venue's policy allows it — issue a refund back to the "
            "original card.\n\n"
            "Refunds go through the theater's own Stripe account, so the money "
            "returns to the buyer the same way it came in. Check your venue's "
            "refund policy article before promising a refund at the counter."
        ),
    ),
    BuiltinArticle(
        slug="discounts-and-promo-codes",
        title="Discounts & promo codes",
        summary="Create percent or fixed-amount codes and see how buyers apply them.",
        category=Category.HOW_TO,
        visibility=Visibility.BOX_OFFICE,
        body=(
            "Create a code under Promo codes: give it a name, choose Percentage off "
            "or Fixed amount off, and set the value. You can add an active window, "
            "a maximum number of redemptions, and a minimum order amount — leave "
            "any of those blank for no limit.\n\n"
            "Buyers enter a code on the cart page, on the item they're checking "
            "out (holds are per performance, so a code is applied per cart item). "
            "A valid code shows the discount right under the subtotal before they "
            "go to Stripe checkout.\n\n"
            "A code only discounts the ticket subtotal — it never reduces a "
            "donation added at checkout. Redemptions are only counted once an "
            "order is actually paid, so an abandoned cart never uses up a "
            "limited code.\n\n"
            "Deactivate a code any time from the Promo codes list. Codes are "
            "retired rather than deleted, so past orders keep an accurate record "
            "of what was applied."
        ),
    ),
    BuiltinArticle(
        slug="donations",
        title="Donations",
        summary="Turn on the general fund, and where donation money shows up.",
        category=Category.HOW_TO,
        visibility=Visibility.MANAGER,
        body=(
            "Turn on donations from the donation settings screen: switch it "
            "active, set a name for the fund, and list a few suggested amounts — "
            "they show as preset buttons to the buyer. You can also add "
            "acknowledgment text (your nonprofit status, EIN, or a thank-you) "
            "that prints on the donation receipt.\n\n"
            "With donations on, buyers see the preset amounts plus a custom "
            "amount field on the cart page, so they can add a donation on top of "
            "a ticket order. There's also a standalone donate page for anyone "
            "who wants to give without buying a ticket.\n\n"
            "Donation totals show up in their own report in the dashboard, with "
            "date filtering and a CSV export, and any donation on an order shows "
            "as its own line on that order's detail page."
        ),
    ),
    BuiltinArticle(
        slug="season-and-flex-passes",
        title="Season & flex passes",
        summary="The difference between season and flex passes, and how buyers use them.",
        category=Category.HOW_TO,
        visibility=Visibility.MANAGER,
        body=(
            "A season pass covers one admission per show in the pass — the buyer "
            "picks which performance of each covered event when they redeem. A "
            "flex pass instead carries a set number of credits that can be spent "
            "on any covered performance, in any combination, until the credits "
            "run out.\n\n"
            "Passes are sold like any other purchase — buyers pick a pass and "
            "check out through Stripe, no separate flow. What they bought (which "
            "events, the validity window, how many credits) is locked in at "
            "purchase, so changing the pass product later never changes what's "
            "already sold.\n\n"
            "To use a pass, a buyer signs in to their account, opens My passes, "
            "and chooses Redeem. That puts them in redeem mode while they shop "
            "normally — any covered performance offers a \"Redeem with pass\" "
            "option in the cart instead of a price, and checkout completes with "
            "no charge.\n\n"
            "Staff can see pass sales and outstanding liability — flex credits "
            "not yet redeemed and their dollar value, plus season admissions "
            "still owed — from the pass sales report."
        ),
    ),
    BuiltinArticle(
        slug="working-the-door",
        title="Working the door: scanning tickets",
        summary="How check-in works and what to do when a ticket won't scan.",
        category=Category.HOW_TO,
        visibility=Visibility.STAFF,
        body=(
            "Open Scan and point the camera at the QR code on the ticket. A green "
            "check means the ticket is valid and now redeemed; a ticket only scans "
            "in once.\n\n"
            "If a ticket won't scan:\n"
            "• Already used — someone already came in on it. Confirm the name on "
            "the order before admitting.\n"
            "• Wrong performance — the ticket is for a different date/time.\n"
            "• No camera — type the order number in Orders to check the buyer in "
            "manually.\n\n"
            "When in doubt, look the order up by the buyer's email in Orders."
        ),
    ),
    BuiltinArticle(
        slug="events-and-performances",
        title="Creating events and performances",
        summary="Set up a show, add dates, and put tickets on sale.",
        category=Category.HOW_TO,
        visibility=Visibility.MANAGER,
        body=(
            "An Event is the show (e.g. \"A Christmas Carol\"); a Performance is one "
            "dated showing of it. Create the event first, then add a performance for "
            "each date.\n\n"
            "For each performance choose a seating mode: General admission (a "
            "capacity number) or Reserved (a seating chart you build under Seating). "
            "Add price tiers, then set the event and performance to Published to put "
            "them on sale on your storefront."
        ),
    ),
    BuiltinArticle(
        slug="team-and-roles",
        title="Your team and what each role can do",
        summary="Owner, Manager, Box office and Scanner — who can do what.",
        category=Category.HOW_TO,
        visibility=Visibility.MANAGER,
        body=(
            "Invite staff under Team and give each person a role. Roles are "
            "cumulative — each one can do everything the role below it can:\n\n"
            "• Owner — everything, including billing and granting the owner role.\n"
            "• Manager — events, seating, orders, and managing the team.\n"
            "• Box office — sell tickets, look up orders, issue refunds, scan.\n"
            "• Scanner — check tickets in at the door.\n\n"
            "Only owners can grant the owner role. Managers can add and adjust "
            "everyone else."
        ),
    ),
    BuiltinArticle(
        slug="email-marketing-and-your-audience",
        title="Email marketing & your audience",
        summary="Consent, segments, and sending a campaign to your buyers.",
        category=Category.HOW_TO,
        visibility=Visibility.MANAGER,
        body=(
            "Buyers opt in to marketing email themselves — there's a checkbox at "
            "checkout on tickets, passes, and donations, and they can change "
            "their mind any time from their own account preferences. Nobody is "
            "added to your list without checking that box.\n\n"
            "The Audience list shows every opted-in guest along with their order "
            "history and lifetime spend, so you can see who you're about to "
            "email before you send anything.\n\n"
            "When you compose a campaign you choose a segment: everyone opted "
            "in, buyers of a specific event, or buyers who've spent at least a "
            "minimum amount. Write a subject and a plain-text body, send "
            "yourself a test copy, then send — delivery happens in the "
            "background and the campaign page tracks sent, failed and pending "
            "counts.\n\n"
            "Every campaign email carries a one-click unsubscribe link, and "
            "unsubscribing is automatic and immediate — no confirmation click "
            "required — though a buyer can resubscribe from the same page if "
            "they change their mind."
        ),
    ),
    BuiltinArticle(
        slug="buyer-faq",
        title="Ticket buyer FAQ",
        summary="Answers to the questions buyers ask most.",
        category=Category.GENERAL,
        visibility=Visibility.PUBLIC,
        body=(
            "How do I get my tickets? Your tickets are emailed to you right after "
            "purchase. You can also find them any time under \"My tickets\" using "
            "the email you bought with.\n\n"
            "I didn't get the email. Check spam, then use \"My tickets\" to have the "
            "link sent again. If it still doesn't arrive, contact the box office.\n\n"
            "Can I get a refund or exchange? That depends on the venue's policy — "
            "see the refund/exchange article on this page, or contact the box "
            "office.\n\n"
            "Do I need to print my ticket? No — the QR code on your phone scans fine "
            "at the door.\n\n"
            "What time should I arrive? Doors usually open ahead of the listed "
            "start time. Latecomers may be seated at a suitable break, at the "
            "house's discretion."
        ),
    ),
    BuiltinArticle(
        slug="venue-policies",
        title="Venue rules & accessibility",
        summary="General house rules and accessibility information.",
        category=Category.VENUE_RULES,
        visibility=Visibility.PUBLIC,
        body=(
            "Please arrive with time to spare so everyone is seated before the "
            "performance begins. Photography, video and recording are generally not "
            "permitted during the show.\n\n"
            "Accessibility: if you have seating, mobility or access needs, contact "
            "the box office before your visit so we can help. Your venue may add its "
            "own detailed rules and accessibility information here."
        ),
    ),
]


def readable_by(membership):
    """Built-in articles visible to a staff Membership's role."""
    allowed = visibilities_readable_by(membership)
    return [a for a in BUILTIN_ARTICLES if a.visibility in allowed]


def public():
    """Built-in articles for the storefront FAQ (PUBLIC only)."""
    return [a for a in BUILTIN_ARTICLES if a.visibility == Visibility.PUBLIC]
