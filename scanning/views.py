import re

from django.contrib import messages
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse

from accounts.permissions import scanner_required
from orders.tokens import sign_token

from .services import redeem_ticket


def _wants_json(request):
    """The in-page camera/manual-loop fetch() sets Accept: application/json
    so it gets a small JSON payload back instead of a full HTML page --
    everything else (most importantly: a phone's stock Camera app opening
    the ticket QR's URL directly, per docs/ARCHITECTURE.md) gets the normal
    server-rendered result page."""
    return request.headers.get("Accept", "").startswith("application/json") or request.GET.get(
        "format"
    ) == "json"


@scanner_required
def scan_home(request):
    """Staff scanner page: an inline camera scanner (BarcodeDetector when
    available, jsQR everywhere else -- see static/js/scanner.js) plus a
    manual token-entry fallback for desktops/denied camera permissions.
    Manual entry recomputes a valid signature server-side (the staffer is
    already authenticated + scanner-role-gated to be on this page at all),
    so there is still exactly one code path -- redeem_ticket() -- that ever
    decides pass/fail, whether the ticket got here via camera, manual entry,
    or a phone's camera app opening the QR's URL directly.

    With JS on, the manual form fetch()es this view with Accept:
    application/json and gets the same JSON verdict the camera loop renders,
    so staff stay on the app-like scan screen. With JS off it falls back to a
    plain POST that redirects to the full scan_redeem result page.
    """
    error = None
    if request.method == "POST":
        wants_json = _wants_json(request)
        # Tokens are uppercase alphanumeric (orders.models.new_token); accept a
        # lowercase paste too by upper()ing first. The regex is just a shape
        # guard so a stray paste can't reach reverse() with a char the
        # <slug:token> route can't build (which would 500) -- a genuinely
        # wrong-but-well-formed code still flows through to redeem_ticket, the
        # single place that decides pass/fail.
        raw_token = request.POST.get("token", "").strip().upper()
        if not raw_token or not re.fullmatch(r"[A-Z0-9]+", raw_token):
            error = "That doesn't look like a valid ticket code."
            if wants_json:
                return JsonResponse(
                    {"ok": False, "reason": "invalid_code", "message": error, "ticket": None}
                )
        else:
            sig = sign_token(raw_token, request.organization.id)
            if wants_json:
                # In-page manual entry: redeem here and hand back the same
                # ScanResult JSON the camera loop renders, so staff never leave
                # the scan screen.
                result = redeem_ticket(
                    organization=request.organization,
                    token=raw_token,
                    sig=sig,
                    scanned_by=request.user,
                )
                return JsonResponse(result.as_dict())
            return redirect(reverse("scan_redeem", args=[raw_token, sig]))

    return render(request, "scanning/scan_home.html", {"error": error})


@scanner_required
def scan_redeem(request, token, sig):
    """GET-only (it's what a QR code's encoded URL resolves to, and what the
    in-page scanner's fetch() hits): verify the signature, then
    lock-check-flip the Ticket inside redeem_ticket(). Both `token` and `sig`
    arrive as URL path segments (see scanning/urls.py). Requires login +
    scanner role like every view in this app -- the QR by itself is not
    sufficient to redeem a ticket; a public/anonymous request never reaches
    this far (accounts.permissions.scanner_required 404s off-tenant hosts,
    sends anonymous users to /login/, and 403s a logged-in user who isn't
    staff at this org or doesn't have scanner+ role).
    """
    result = redeem_ticket(
        organization=request.organization, token=token, sig=sig, scanned_by=request.user
    )

    if _wants_json(request):
        return JsonResponse(result.as_dict())

    if result.ok:
        messages.success(request, result.message)
    else:
        messages.error(request, result.message)
    return render(request, "scanning/scan_result.html", {"result": result})
