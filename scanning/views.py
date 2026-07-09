import uuid

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
    already authenticated + scanner-role-gated to be on this page at all)
    and bounces to the normal redeem URL, so there is still exactly one
    code path -- scan_redeem()/redeem_ticket() -- that ever decides
    pass/fail, whether the ticket got here via camera, manual entry, or a
    phone's camera app opening the QR's URL directly.
    """
    error = None
    if request.method == "POST":
        raw_token = request.POST.get("token", "").strip()
        try:
            token = uuid.UUID(raw_token)
        except ValueError:
            error = "That doesn't look like a valid ticket code."
        else:
            sig = sign_token(token, request.organization.id)
            return redirect(f"{reverse('scan_redeem', args=[token])}?sig={sig}")

    return render(request, "scanning/scan_home.html", {"error": error})


@scanner_required
def scan_redeem(request, token):
    """GET-only (it's what a QR code's encoded URL resolves to, and what the
    in-page scanner's fetch() hits): verify the signature, then
    lock-check-flip the Ticket inside redeem_ticket(). Requires login +
    scanner role like every view in this app -- the QR by itself is not
    sufficient to redeem a ticket; a public/anonymous request never reaches
    this far (accounts.permissions.scanner_required 404s off-tenant hosts,
    sends anonymous users to /login/, and 403s a logged-in user who isn't
    staff at this org or doesn't have scanner+ role).
    """
    sig = request.GET.get("sig")
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
