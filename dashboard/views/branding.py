import base64
import os
import re

from django.conf import settings
from django.contrib import messages
from django.core.files.base import ContentFile
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from accounts import throttle
from accounts.permissions import manager_required
from tenants.color_extraction import ColorDeriveError, derive_scheme_from_url
from tenants.color_generator import scheme_from_primary
from tenants.color_schemes import COLOR_ROLES, HEX_COLOR_RE
from tenants.fonts import FONTS
from tenants.logo_bg import (
    BackgroundRemovalUnavailable,
    LogoBackgroundError,
    remove_logo_background,
)
from tenants.models import ColorScheme

from ..forms import BrandingForm, ColorSchemeForm


# --- branding / color schemes (manager+) -----------------------------------


def _branding_context(request, **extra):
    """Shared context for the branding page: the logo+colors form, the built-in
    presets, and this tenant's own saved schemes. `extra` lets a POST handler
    layer on a derived-palette preview or a bound form with errors."""
    organization = request.organization
    context = {
        "organization": organization,
        "branding_form": BrandingForm(instance=organization),
        # Default "save these colors as a scheme" form, pre-filled with the
        # org's current palette (palette role keys == ColorSchemeForm fields).
        # A POST handler can override with a bound (errored) or derived form.
        "scheme_form": ColorSchemeForm(initial=organization.palette, organization=organization),
        "presets": ColorScheme.objects.filter(is_preset=True),
        "custom_schemes": ColorScheme.objects.filter(organization=organization),
        "roles": COLOR_ROLES,
        "current_palette": organization.palette,
        # key -> CSS stack, so the live-preview JS can resolve a font <select>'s
        # value to an actual font-family without another request.
        "font_stacks": {key: spec["stack"] for key, spec in FONTS.items()},
        # A pending background-removal result (base64 PNG in the session, set by
        # the preview action) surfaced as a data URI so the template can show the
        # cut-out for Save/Discard without a second request. None = nothing to
        # confirm -> the plain "Remove background" button shows instead.
        "logo_bg_preview": _pending_logo_bg_preview(request),
    }
    context.update(extra)
    return context


def _pending_logo_bg_preview(request):
    """The session's pending background-removal PNG as a data URI, or None."""
    preview_b64 = request.session.get(LOGO_BG_PREVIEW_SESSION_KEY)
    return f"data:image/png;base64,{preview_b64}" if preview_b64 else None


def _apply_scheme_to_org(request, scheme):
    organization = request.organization
    organization.apply_color_scheme(scheme)
    messages.success(request, f"Applied “{scheme.name}” to your storefront.")


@manager_required
def branding(request):
    """Logo + six-role brand palette for the storefront. GET shows the current
    colors, the built-in preset gallery, and the tenant's saved custom schemes.
    POST dispatches on an `action` field:

    - save_colors:  save the logo + fonts + hand-picked colors (BrandingForm).
      This is the one editor's primary "Save branding" button.
    - apply_scheme: copy a preset OR one of this tenant's own schemes onto the
      org (scheme lookup is scoped to presets + this org, so a tampered pk
      can't apply another tenant's scheme).
    - save_scheme:  save the editor's six colors as a new named custom scheme
      (the same form's "Save as scheme" button; colors post under the org field
      names, normalized back to role keys here).
    - delete_scheme: delete one of this tenant's own custom schemes.

    Colors are stored on the Organization (the storefront's source of truth);
    schemes are reusable templates, never the live render source.
    """
    organization = request.organization
    action = request.POST.get("action") if request.method == "POST" else None

    if action == "save_colors":
        form = BrandingForm(request.POST, request.FILES, instance=organization)
        if form.is_valid():
            form.save()
            messages.success(request, "Branding saved.")
            return redirect("dashboard_branding")
        return render(request, "dashboard/branding.html", _branding_context(request, branding_form=form))

    if action == "apply_scheme":
        scheme = get_object_or_404(
            ColorScheme.objects.filter(Q(is_preset=True) | Q(organization=organization)),
            pk=request.POST.get("scheme_id"),
        )
        _apply_scheme_to_org(request, scheme)
        return redirect("dashboard_branding")

    if action == "save_scheme":
        # The unified editor posts colors under the Organization field names
        # (primary_color, …); accept the scheme-role names too so the derive
        # flow and a direct role-keyed POST both work. ROLE key wins if present.
        scheme_data = {
            "name": request.POST.get("name", ""),
            **{
                role: request.POST.get(role) or request.POST.get(org_field) or ""
                for role, _label, org_field in COLOR_ROLES
            },
        }
        form = ColorSchemeForm(scheme_data, organization=organization)
        if form.is_valid():
            scheme = form.save()
            messages.success(request, f"Saved “{scheme.name}” to your schemes.")
            if request.POST.get("apply_after_save"):
                _apply_scheme_to_org(request, scheme)
            return redirect("dashboard_branding")
        # Re-render with the name error surfaced and the manager's in-progress
        # logo/font/color edits preserved in the one editor (bound, not saved).
        branding_form = BrandingForm(request.POST, request.FILES, instance=organization)
        return render(
            request,
            "dashboard/branding.html",
            _branding_context(request, branding_form=branding_form, scheme_form=form),
        )

    if action == "delete_scheme":
        scheme = get_object_or_404(
            ColorScheme, pk=request.POST.get("scheme_id"), organization=organization
        )
        name = scheme.name
        scheme.delete()
        messages.success(request, f"Deleted “{name}”.")
        return redirect("dashboard_branding")

    return render(request, "dashboard/branding.html", _branding_context(request))


# Cap on the homepage URL a manager can submit -- generous for real URLs, but
# bounds the input a hostile client can throw at the fetch/guard.
MAX_DERIVE_URL_LEN = 2000


def _derive_error(request, is_ajax, message, status=400, retry_after=None):
    """A derive failure, shaped for the caller: JSON for the inline (fetch)
    flow, a flashed message + redirect for the no-JS fallback. `retry_after`
    (seconds) rides along on rate-limit refusals so the page can count down."""
    if is_ajax:
        payload = {"ok": False, "error": message}
        if retry_after is not None:
            payload["retry_after"] = retry_after
        return JsonResponse(payload, status=status)
    messages.error(request, message)
    return redirect("dashboard_branding")


@manager_required
@require_POST
def branding_derive(request):
    """Run the derive-from-homepage agent (tenants.color_extraction) on a URL
    the manager enters and return the proposed palette.

    Called inline by the branding page's JS (X-Requested-With), it answers with
    JSON the page loads straight into the color pickers + preview -- the manager
    never leaves the page. Without JS it falls back to re-rendering branding.html
    with the palette pre-filled. Either way a fetch/parse failure is a clean
    message, never a 500.

    The endpoint is expensive (external fetch + optional headless render + a
    Claude call), so it's rate-limited per org (throttle.over_limit) and the URL
    length is capped; the agent itself is SSRF-guarded (tenants.color_extraction).
    """
    is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"
    org_id = str(request.organization.pk)
    url = (request.POST.get("url") or "").strip()
    if not url:
        return _derive_error(request, is_ajax, "Enter your homepage URL first.")
    if len(url) > MAX_DERIVE_URL_LEN:
        return _derive_error(request, is_ajax, "That web address is too long to read.")

    # Cooldown first (a still-warm org shouldn't spend a window slot just to be
    # refused), then the fixed-window cap.
    cooling = throttle.cooldown_remaining("derive", org_id)
    if cooling > 0:
        return _derive_error(
            request, is_ajax,
            f"Just a moment — you can derive again in {cooling}s.",
            status=429, retry_after=cooling,
        )
    if throttle.over_limit(
        "derive", org_id,
        settings.DERIVE_RATELIMIT_MAX, settings.DERIVE_RATELIMIT_WINDOW_SECONDS,
    ):
        return _derive_error(
            request, is_ajax,
            "You’ve derived a lot of palettes in a short time. Give it a few minutes and try again.",
            status=429,
        )

    try:
        derived = derive_scheme_from_url(url)
    except ColorDeriveError as exc:
        # A failed fetch is cheap and often a fixable typo -- don't start the
        # cooldown, so the manager can correct the URL and retry immediately.
        return _derive_error(request, is_ajax, str(exc))

    # A real derive ran: start the cooldown so the next one can't fire on its heels.
    throttle.start_cooldown("derive", org_id, settings.DERIVE_COOLDOWN_SECONDS)

    if is_ajax:
        return JsonResponse(
            {
                "ok": True,
                "name": derived["name"],
                "roles": derived["roles"],
                "candidates": derived["candidates"],
                "method": derived.get("method", "heuristic"),
                "source_url": derived["source_url"],
                "cooldown": settings.DERIVE_COOLDOWN_SECONDS,
            }
        )

    # No-JS fallback: pre-fill the one editor with the derived colors -- the six
    # pickers (on the Organization field names) and the optional scheme-name
    # input. Saving then runs the ordinary save_colors / save_scheme POST paths.
    organization = request.organization
    scheme_form = ColorSchemeForm(
        initial={"name": derived["name"], **derived["roles"]}, organization=organization
    )
    branding_form = BrandingForm(
        instance=organization,
        initial={
            org_field: derived["roles"][role] for role, _label, org_field in COLOR_ROLES
        },
    )
    return render(
        request,
        "dashboard/branding.html",
        _branding_context(
            request, scheme_form=scheme_form, branding_form=branding_form, derived=derived
        ),
    )


@manager_required
@require_POST
def branding_harmonize(request):
    """Build a full six-role scheme from a single primary color (the branding
    "harmonize" button) using the same rules as the catalog
    (tenants.color_generator.scheme_from_primary), and return it as JSON for the
    page's JS to load into the color pickers + live preview. Nothing is saved --
    it's a client-side suggestion until the manager hits Save."""
    primary = (request.POST.get("primary") or "").strip()
    if not re.match(HEX_COLOR_RE, primary):
        return JsonResponse(
            {"ok": False, "error": "Pick a valid primary color first."}, status=400
        )
    return JsonResponse({"ok": True, "roles": scheme_from_primary(primary)})


# Session key holding a pending background-removal result (base64 PNG) that the
# manager has been shown but not yet accepted. Sessions here are DB-backed (not
# the cookie backend), so a small PNG rides along fine. Kept out of the model so
# a discarded preview never touches the live logo.
LOGO_BG_PREVIEW_SESSION_KEY = "logo_bg_preview"


@manager_required
@require_POST
def branding_logo_remove_bg(request):
    """Background removal as a PREVIEW-then-confirm flow, so the manager sees the
    cut-out result before it replaces the live logo (a logo is load-bearing --
    overwriting it sight-unseen was the wrong default). Dispatches on `action`:

    - preview (default): run rembg on the STORED logo and stash the transparent
      PNG in the session, then redirect back; _branding_context surfaces it as a
      side-by-side "before / after" with Save / Discard. The heavy step, so it's
      rate-limited per org (fixed-window cap + short cooldown) and a missing
      rembg dependency is a clean "unavailable" message, never a 500.
    - confirm: write the stashed preview over the logo (as `<stem>-nobg.png`).
    - discard: drop the stashed preview; the live logo is untouched.

    Every path redirects (POST/redirect/GET), so a refresh never re-runs the
    model or re-saves."""
    organization = request.organization
    action = request.POST.get("action", "preview")

    if action == "discard":
        request.session.pop(LOGO_BG_PREVIEW_SESSION_KEY, None)
        return redirect("dashboard_branding")

    if action == "confirm":
        preview_b64 = request.session.get(LOGO_BG_PREVIEW_SESSION_KEY)
        if not preview_b64 or not organization.logo:
            # Preview expired, already applied, or the logo was cleared meanwhile.
            request.session.pop(LOGO_BG_PREVIEW_SESSION_KEY, None)
            messages.error(request, "That preview expired — run “Remove background” again.")
            return redirect("dashboard_branding")
        cleaned = base64.b64decode(preview_b64)
        stem = os.path.splitext(os.path.basename(organization.logo.name))[0].removesuffix("-nobg")
        organization.logo.save(f"{stem}-nobg.png", ContentFile(cleaned), save=True)
        request.session.pop(LOGO_BG_PREVIEW_SESSION_KEY, None)
        messages.success(request, "Saved your logo with the background removed.")
        return redirect("dashboard_branding")

    # --- action == "preview": run the model and stash the result ---
    if not organization.logo:
        messages.error(request, "Upload a logo first, then you can remove its background.")
        return redirect("dashboard_branding")

    org_id = str(organization.pk)
    cooling = throttle.cooldown_remaining("logo_bg", org_id)
    if cooling > 0:
        messages.error(request, f"Just a moment — you can try again in {cooling}s.")
        return redirect("dashboard_branding")
    if throttle.over_limit(
        "logo_bg", org_id,
        settings.LOGO_BG_RATELIMIT_MAX, settings.LOGO_BG_RATELIMIT_WINDOW_SECONDS,
    ):
        messages.error(
            request,
            "You’ve run background removal several times just now. "
            "Give it a few minutes and try again.",
        )
        return redirect("dashboard_branding")

    try:
        organization.logo.open("rb")
        try:
            raw = organization.logo.read()
        finally:
            organization.logo.close()
        cleaned = remove_logo_background(raw)
    except BackgroundRemovalUnavailable as exc:
        messages.error(request, str(exc))
        return redirect("dashboard_branding")
    except LogoBackgroundError as exc:
        # A processing failure is cheap and often image-specific -- don't burn
        # the cooldown, so a manager can immediately try a different file.
        messages.error(request, str(exc))
        return redirect("dashboard_branding")

    throttle.start_cooldown("logo_bg", org_id, settings.LOGO_BG_COOLDOWN_SECONDS)
    request.session[LOGO_BG_PREVIEW_SESSION_KEY] = base64.b64encode(cleaned).decode("ascii")
    messages.success(request, "Here’s your logo with the background removed — save it or discard it.")
    return redirect("dashboard_branding")


@manager_required
@require_POST
def branding_logo_remove(request):
    """Delete the org's logo entirely (the storefront falls back to its name).
    An explicit action button on the branding page -- the reason the logo field
    uses a plain file input instead of Django's ClearableFileInput + its Clear
    checkbox. Also drops any pending background-removal preview, since it's about
    a logo that's about to be gone."""
    organization = request.organization
    request.session.pop(LOGO_BG_PREVIEW_SESSION_KEY, None)
    if organization.logo:
        organization.logo.delete(save=True)  # removes the stored file, clears the field, saves
        messages.success(request, "Removed your logo.")
    else:
        messages.info(request, "There’s no logo to remove.")
    return redirect("dashboard_branding")
