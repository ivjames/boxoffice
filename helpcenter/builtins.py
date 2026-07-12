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
