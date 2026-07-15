"""AI-assisted seating-chart parsing: turn an uploaded IMAGE or PDF of a
house's seating chart into a real, editable SeatingChart.

The pipeline has three stages, each independently testable:

1. `parse_chart_file(data, media_type)` -- sends the file to the Claude API
   (vision: images and PDFs are both first-class input) and asks for a
   *parametric* "chart spec": one entry per section carrying the SAME layout
   params `Section` persists (rows/seats_per_row/origin/pitch/rotation/arc/
   offset/numbering). Structured output (`output_config.format` with a JSON
   schema) guarantees the reply is valid JSON in exactly that shape.
2. `validate_chart_spec(spec)` -- defensive normalisation of the model's
   output: clamps counts, coerces types, falls back to model-choice defaults
   for unknown scheme values, dedupes section names. Never trust generated
   JSON to be in-range just because it's schema-valid.
3. `build_chart_from_spec(venue, spec)` -- persists the chart: creates the
   Sections and generates their Seats server-side via venues.generation
   (compute_row_counts + generate_seats), the same authoritative formulas
   the live chart editor saves through.

WHY PARAMS, NOT SEAT DUMPS: chart_io.import_chart_data can already ingest
explicit per-seat x/y -- but the dashboard editor (docs/EDITOR.md) is
param-driven, and Save always recomputes every seat's x/y from its
section's params. A chart imported as raw coordinates would be silently
reshaped on the first editor save. Parsing INTO the params means the result
is a first-class citizen of the editor: staff review the AI's guess on the
live canvas and drag/rotate/re-pitch from there. Irregular details (aisle
gaps, wheelchair positions) ride along as removed_seats/accessible_seats
identity overrides, which regeneration already preserves.

Tenant scoping: like chart_io, `build_chart_from_spec` is handed an
already-resolved `venue` and always stamps `venue.organization` -- nothing
in the parsed JSON can plant rows in another tenant.

Configuration: `ANTHROPIC_API_KEY` (env/.env; the SDK's own env resolution
is the fallback) and `CHART_PARSING_MODEL` (defaults to claude-opus-4-8).
"""

import json
import logging

from django.conf import settings
from django.db import transaction

from .generation import compute_row_counts, generate_seats
from .models import SeatingChart, Section

# Uploads we'll forward to the API: Claude accepts these image types as
# `image` content blocks and PDFs as `document` blocks (base64, no beta).
SUPPORTED_MEDIA_TYPES = {
    "image/png",
    "image/jpeg",
    "image/gif",
    "image/webp",
    "application/pdf",
}

_EXTENSION_MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".pdf": "application/pdf",
}

# Hard ceilings on what a parsed section may generate -- a hallucinated
# "10000 rows" must not bulk_create half a million Seat rows.
MAX_ROWS = 100
MAX_SEATS_PER_ROW = 100
MAX_SECTIONS = 40

# 20 MB: comfortably above any real chart scan, safely below the API's
# 32 MB request limit once base64 overhead (~4/3) is added.
MAX_UPLOAD_BYTES = 20 * 1024 * 1024


logger = logging.getLogger(__name__)


class ChartParsingError(Exception):
    """Raised anywhere the upload -> spec -> chart pipeline can't proceed.
    Message is safe to show directly to dashboard staff / command output."""


def media_type_for_upload(filename, content_type=None):
    """The API media type for an uploaded chart file, or None if the file
    isn't a supported image/PDF. Prefers the browser-supplied content_type
    when it's one we support; falls back to the filename extension (curl
    and some browsers send application/octet-stream)."""
    if content_type in SUPPORTED_MEDIA_TYPES:
        return content_type
    name = (filename or "").lower()
    for extension, media_type in _EXTENSION_MEDIA_TYPES.items():
        if name.endswith(extension):
            return media_type
    return None


# --- stage 1: file -> spec (Claude API) -----------------------------------

# The structured-output JSON schema. Mirrors Section's params exactly so
# stage 3 is a straight field copy. Structured outputs forbid numeric
# range constraints, so ranges are enforced in validate_chart_spec instead.
_SECTION_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "Section name as printed on the chart, e.g. 'Orchestra Left'."},
        "tier": {"type": "string", "description": "Grouping label like Orchestra/Mezzanine/Balcony, or '' if none."},
        "rows": {"type": "integer", "description": "Number of rows in this section."},
        "seats_per_row": {"type": "integer", "description": "Typical seats per row (the mode, if rows are ragged)."},
        "origin_x": {"type": "number"},
        "origin_y": {"type": "number"},
        "rotation": {"type": "number", "description": "Degrees clockwise the section is rotated. 0 for a straight block."},
        "seat_pitch": {"type": "number", "description": "Spacing between adjacent seats. Use 1.0 unless clearly different."},
        "row_pitch": {"type": "number", "description": "Spacing between rows. Use 1.0 unless clearly different."},
        "arc_radius": {
            "anyOf": [{"type": "number"}, {"type": "null"}],
            "description": "For curved/fanned sections: the front row's arc radius in the same units (typically 15-40). null for straight sections.",
        },
        "offset_mode": {"type": "string", "enum": ["repeated", "alternating"]},
        "row_x_offset": {"type": "number", "description": "Per-row x stagger. 0 for a plain grid; positive with offset_mode=repeated for a raked/trapezoid side section."},
        "alt_row_seat_delta": {"type": "integer", "description": "alternating mode only: seats added/dropped on every other row. Usually 0."},
        "numbering_scheme": {
            "type": "string",
            "enum": ["sequential", "odd_desc_left", "even_asc_right", "hundreds", "hundreds_flat"],
        },
        "row_label_scheme": {"type": "string", "enum": ["skip_io", "all_letters"]},
        "row_label_start": {
            "type": "integer",
            "description": (
                "Index into the row-label sequence for this section's FIRST row: 0 = A "
                "(the usual case). Use it when a section continues the house's letter "
                "sequence -- e.g. a Parterre whose first row is N behind an A-M Orchestra "
                "is 12 under skip_io (A=0, B=1, ... H=7, J=8, ... N=12)."
            ),
        },
        "removed_seats": {
            "type": "array",
            "items": {"type": "array", "items": {"type": "string"}},
            "description": "[row_label, seat_number] pairs for seats missing from the printed chart (aisle gaps, tech booth cut-outs). Empty list if none.",
        },
        "accessible_seats": {
            "type": "array",
            "items": {"type": "array", "items": {"type": "string"}},
            "description": "[row_label, seat_number] pairs marked wheelchair/accessible on the chart. Empty list if none.",
        },
    },
    "required": [
        "name", "tier", "rows", "seats_per_row", "origin_x", "origin_y", "rotation",
        "seat_pitch", "row_pitch", "arc_radius", "offset_mode", "row_x_offset",
        "alt_row_seat_delta", "numbering_scheme", "row_label_scheme", "row_label_start",
        "removed_seats", "accessible_seats",
    ],
    "additionalProperties": False,
}

CHART_SPEC_SCHEMA = {
    "type": "object",
    "properties": {
        "chart_name": {"type": "string", "description": "A short name for this chart, e.g. 'Main house'."},
        "sections": {"type": "array", "items": _SECTION_SCHEMA},
    },
    "required": ["chart_name", "sections"],
    "additionalProperties": False,
}

_PARSE_PROMPT = """\
This is a theater seating chart. Extract every seating section into the JSON \
schema you were given, using this coordinate system: x grows to the right, y \
grows AWAY from the stage (toward the back of the house), one seat-width = \
1.0 units. Each section's origin_x/origin_y is its front-left corner (the \
leftmost seat of the row nearest the stage). Place sections so they don't \
overlap and their relative positions match the chart -- e.g. a left box at \
smaller x than the center block, a balcony at larger y than the orchestra.

Guidelines:
- Houses are usually split into blocks by aisles (e.g. Left/Center/Right per \
tier). Parse each block as its own section.
- seats_per_row is the section's WIDEST row. For every shorter row, list the \
printed numbers that are missing (relative to that widest row's numbering) in \
removed_seats. Shorter rows almost always hug the aisle, so the missing seats \
are the WALL-side ones: the highest odd numbers for odd_desc_left, the \
highest even numbers for even_asc_right, the highest numbers for hundreds_flat \
-- unless the chart clearly shows a mid-row gap, in which case remove exactly \
the printed gap.
- rows counts every row POSITION front to back, including a row that is \
entirely absent from a section (e.g. a cross-aisle where the center block has \
no row D but the sides do): keep the position and put ALL of that row's seats \
in removed_seats, so the rows behind it keep their correct letters.
- Row labels run front to back per row_label_scheme starting at \
row_label_start; seat numbers follow numbering_scheme, so with 'sequential' \
the leftmost seat of every row is "1".
- Curved/fanned rows: set arc_radius (front-row radius, in seat units -- \
gentler curve = larger radius). Straight rows: arc_radius null.
- Diagonal/raked side sections: use rotation (whole-block tilt) and/or \
row_x_offset with offset_mode 'repeated' (each row shifted further than the \
last, making a trapezoid).
- numbering_scheme: read the printed seat numbers. Odd numbers descending \
toward the aisle = odd_desc_left; ascending evens = even_asc_right; rows \
numbered 101/201/301 = hundreds; EVERY row restarting at 101 (a common \
center-block style) = hundreds_flat; plain 1,2,3 = sequential.
- row_label_scheme: skip_io if rows jump from H to J (no I), otherwise \
all_letters.
- row_label_start: 0 when the section's first row is labelled A. When a \
section continues the house's letter sequence (e.g. a mezzanine whose first \
row is N behind an A-M orchestra), set the index of its first letter in the \
scheme's sequence (skip_io: A=0 ... H=7, J=8 ... N=12 ... V=19).
- Mark wheelchair symbols in accessible_seats.
- Ignore the stage, legends, lobbies and other non-seat elements.
"""


def _get_client():
    """Anthropic client factory -- isolated so tests can patch it. Uses the
    key from Django settings when set, otherwise the SDK's own environment
    resolution (ANTHROPIC_API_KEY et al.)."""
    try:
        import anthropic
    except ImportError as exc:  # pragma: no cover - dependency is in requirements.txt
        raise ChartParsingError(
            "The 'anthropic' package is not installed. Run: pip install -r requirements.txt"
        ) from exc
    api_key = getattr(settings, "ANTHROPIC_API_KEY", "") or None
    try:
        return anthropic.Anthropic(api_key=api_key)
    except TypeError as exc:
        # The SDK raises TypeError at construction when it can't resolve ANY
        # credential (no settings key, no env var, no profile).
        raise ChartParsingError(
            "Chart parsing isn't configured: no Anthropic API key is set "
            "(set ANTHROPIC_API_KEY in the environment or .env)."
        ) from exc


def _content_block(data_b64, media_type):
    """The API content block for the uploaded file: PDFs go in a `document`
    block, images in an `image` block -- both base64, both natively
    supported by the vision models."""
    source = {"type": "base64", "media_type": media_type, "data": data_b64}
    if media_type == "application/pdf":
        return {"type": "document", "source": source}
    return {"type": "image", "source": source}


def parse_chart_file(data, media_type):
    """Send the file bytes to the Claude API and return a validated chart
    spec dict (see CHART_SPEC_SCHEMA / validate_chart_spec) with the API's
    token accounting attached as spec["usage"] (see _usage_dict /
    describe_usage). Raises ChartParsingError for every failure mode --
    unsupported type, oversize file, API errors, refusals, truncated
    output."""
    import base64

    if media_type not in SUPPORTED_MEDIA_TYPES:
        raise ChartParsingError(
            "Unsupported file type. Upload a PNG, JPEG, GIF, WebP image or a PDF."
        )
    if len(data) > MAX_UPLOAD_BYTES:
        raise ChartParsingError("File is too large (20 MB max).")
    if not data:
        raise ChartParsingError("The uploaded file is empty.")

    try:
        import anthropic
    except ImportError as exc:  # pragma: no cover
        raise ChartParsingError(
            "The 'anthropic' package is not installed. Run: pip install -r requirements.txt"
        ) from exc

    client = _get_client()
    model = getattr(settings, "CHART_PARSING_MODEL", "claude-opus-4-8")
    try:
        response = client.messages.create(
            model=model,
            max_tokens=16000,
            thinking={"type": "adaptive"},
            output_config={"format": {"type": "json_schema", "schema": CHART_SPEC_SCHEMA}},
            messages=[
                {
                    "role": "user",
                    "content": [
                        _content_block(base64.standard_b64encode(data).decode("ascii"), media_type),
                        {"type": "text", "text": _PARSE_PROMPT},
                    ],
                }
            ],
        )
    except TypeError as exc:
        # The SDK raises TypeError at request time when it can't resolve ANY
        # credential (no settings key, no env var, no profile).
        raise ChartParsingError(
            "Chart parsing isn't configured: no Anthropic API key is set "
            "(set ANTHROPIC_API_KEY in the environment or .env)."
        ) from exc
    except anthropic.AuthenticationError as exc:
        raise ChartParsingError(
            "Chart parsing isn't configured: the Anthropic API key is missing or invalid "
            "(set ANTHROPIC_API_KEY)."
        ) from exc
    except anthropic.APIStatusError as exc:
        raise ChartParsingError(f"The parsing service returned an error ({exc.status_code}). Try again.") from exc
    except anthropic.APIConnectionError as exc:
        raise ChartParsingError("Couldn't reach the parsing service. Try again.") from exc

    if response.stop_reason == "refusal":
        raise ChartParsingError("The parsing service declined to process this file.")
    if response.stop_reason == "max_tokens":
        raise ChartParsingError(
            "The chart is too complex to parse in one pass -- try a simpler or cropped image."
        )

    text = next((block.text for block in response.content if block.type == "text"), None)
    if not text:
        raise ChartParsingError("The parsing service returned no result. Try again.")
    try:
        spec = json.loads(text)
    except ValueError as exc:
        raise ChartParsingError("The parsing service returned an unreadable result. Try again.") from exc

    spec = validate_chart_spec(spec)
    spec["usage"] = _usage_dict(model, response)
    logger.info(
        "Parsed seating chart (%s): %s section(s), input_tokens=%s output_tokens=%s "
        "cache_read=%s cache_creation=%s",
        model,
        len(spec["sections"]),
        spec["usage"].get("input_tokens"),
        spec["usage"].get("output_tokens"),
        spec["usage"].get("cache_read_input_tokens"),
        spec["usage"].get("cache_creation_input_tokens"),
    )
    return spec


def _usage_dict(model, response):
    """The API's token accounting for one parse, as a plain dict:
    {"model", "input_tokens", "output_tokens", "cache_read_input_tokens",
    "cache_creation_input_tokens"} -- absent/None fields stay None so
    callers can render "unknown" honestly rather than a fake 0. Attached to
    the spec as spec["usage"] (validate_chart_spec ignores unknown keys, so
    a spec with usage still round-trips through build_chart_from_spec)."""
    usage = getattr(response, "usage", None)
    return {
        "model": model,
        "input_tokens": getattr(usage, "input_tokens", None),
        "output_tokens": getattr(usage, "output_tokens", None),
        "cache_read_input_tokens": getattr(usage, "cache_read_input_tokens", None),
        "cache_creation_input_tokens": getattr(usage, "cache_creation_input_tokens", None),
    }


def describe_usage(usage):
    """One human-readable line for spec["usage"] -- shared by the dashboard
    flash message and the management command ('claude-opus-4-8: 4,182
    tokens in, 1,905 out'). Returns "" when there's nothing to report (no
    usage on the response, e.g. a mocked client)."""
    if not usage or usage.get("input_tokens") is None:
        return ""
    parts = [f"{usage['input_tokens']:,} tokens in"]
    if usage.get("output_tokens") is not None:
        parts.append(f"{usage['output_tokens']:,} out")
    if usage.get("cache_read_input_tokens"):
        parts.append(f"{usage['cache_read_input_tokens']:,} cached")
    return f"{usage.get('model', 'API')}: {', '.join(parts)}"


# --- stage 2: defensive normalisation --------------------------------------


def _clamp_int(value, low, high, default):
    try:
        value = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, value))


def _as_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _seat_identity_list(value):
    """Normalise removed_seats/accessible_seats to the [[row_label, number],
    ...] string-pair shape Section stores; silently drops malformed entries."""
    identities = []
    if not isinstance(value, list):
        return identities
    for entry in value:
        if isinstance(entry, (list, tuple)) and len(entry) == 2:
            identities.append([str(entry[0]), str(entry[1])])
    return identities


def validate_chart_spec(spec):
    """Normalise a raw parsed spec into something build_chart_from_spec can
    trust: every count clamped, every float coerced, every enum checked,
    section names non-empty and unique. Raises ChartParsingError if there's
    nothing usable (no sections)."""
    if not isinstance(spec, dict):
        raise ChartParsingError("The parsed result wasn't a chart spec.")

    raw_sections = spec.get("sections")
    if not isinstance(raw_sections, list) or not raw_sections:
        raise ChartParsingError("No seating sections could be identified in this file.")

    numbering_values = {c.value for c in Section.NumberingScheme}
    row_label_values = {c.value for c in Section.RowLabelScheme}
    offset_values = {c.value for c in Section.OffsetMode}

    sections = []
    seen_names = set()
    for index, raw in enumerate(raw_sections[:MAX_SECTIONS]):
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or "").strip() or f"Section {index + 1}"
        base_name = name
        suffix = 2
        while name.lower() in seen_names:
            name = f"{base_name} ({suffix})"
            suffix += 1
        seen_names.add(name.lower())

        arc_radius = raw.get("arc_radius")
        arc_radius = _as_float(arc_radius, 0.0) if arc_radius is not None else None
        if arc_radius is not None and arc_radius <= 0:
            arc_radius = None

        numbering = raw.get("numbering_scheme")
        row_labels = raw.get("row_label_scheme")
        offset_mode = raw.get("offset_mode")
        sections.append(
            {
                "name": name[:255],
                "tier": str(raw.get("tier") or "")[:100],
                "rows": _clamp_int(raw.get("rows"), 1, MAX_ROWS, 4),
                "seats_per_row": _clamp_int(raw.get("seats_per_row"), 1, MAX_SEATS_PER_ROW, 8),
                "origin_x": _as_float(raw.get("origin_x")),
                "origin_y": _as_float(raw.get("origin_y")),
                "rotation": _as_float(raw.get("rotation")),
                "seat_pitch": _as_float(raw.get("seat_pitch"), 1.0) or 1.0,
                "row_pitch": _as_float(raw.get("row_pitch"), 1.0) or 1.0,
                "arc_radius": arc_radius,
                "offset_mode": offset_mode if offset_mode in offset_values else Section.OffsetMode.REPEATED,
                "row_x_offset": _as_float(raw.get("row_x_offset")),
                "alt_row_seat_delta": _clamp_int(raw.get("alt_row_seat_delta"), -MAX_SEATS_PER_ROW, MAX_SEATS_PER_ROW, 0),
                "numbering_scheme": numbering if numbering in numbering_values else Section.NumberingScheme.SEQUENTIAL,
                "row_label_scheme": row_labels if row_labels in row_label_values else Section.RowLabelScheme.SKIP_IO,
                "row_label_start": _clamp_int(raw.get("row_label_start"), 0, 2 * MAX_ROWS, 0),
                "removed_seats": _seat_identity_list(raw.get("removed_seats")),
                "accessible_seats": _seat_identity_list(raw.get("accessible_seats")),
            }
        )

    if not sections:
        raise ChartParsingError("No seating sections could be identified in this file.")

    return {
        "chart_name": (str(spec.get("chart_name") or "").strip() or "Parsed chart")[:255],
        "sections": sections,
    }


# --- stage 3: spec -> persisted chart --------------------------------------


def build_chart_from_spec(venue, spec, *, name=None, replace=False):
    """Create (or with replace=True overwrite -- same semantics as
    chart_io.import_chart_data) a SeatingChart on `venue` from a validated
    spec: one parametric Section per spec entry, seats generated by
    venues.generation from those exact params, so the result opens in the
    live chart editor as if staff had built it there. Returns the chart."""
    from orders.models import Ticket  # local import: orders imports venues, not vice versa

    spec = validate_chart_spec(spec)
    chart_name = (name or spec["chart_name"]).strip()[:255] or "Parsed chart"
    organization = venue.organization

    with transaction.atomic():
        existing = SeatingChart.objects.filter(
            organization=organization, venue=venue, name=chart_name
        ).first()
        if existing is not None:
            if not replace:
                raise ChartParsingError(
                    f"A seating chart named {chart_name!r} already exists on {venue} "
                    f"(id={existing.pk}). Pass replace=True (or pick another name) to overwrite it."
                )
            live_ticket_seats = (
                Ticket.objects.filter(seat__section__chart=existing)
                .exclude(status=Ticket.Status.VOID)
                .count()
            )
            if live_ticket_seats:
                raise ChartParsingError(
                    f"Chart {chart_name!r} (id={existing.pk}) has {live_ticket_seats} seat(s) with "
                    "a live ticket issued; refusing to overwrite it. Void/refund those tickets "
                    "first, or import under a different name."
                )
            chart = existing
            chart.sections.all().delete()  # cascades to their Seats
        else:
            chart = SeatingChart.objects.create(
                organization=organization, venue=venue, name=chart_name
            )

        for ordering, section_spec in enumerate(spec["sections"]):
            section = Section.objects.create(
                organization=organization,
                chart=chart,
                ordering=ordering,
                **{
                    field: section_spec[field]
                    for field in (
                        "name", "tier", "rows", "seats_per_row", "origin_x", "origin_y",
                        "rotation", "seat_pitch", "row_pitch", "arc_radius", "offset_mode",
                        "row_x_offset", "alt_row_seat_delta", "numbering_scheme",
                        "row_label_scheme", "row_label_start", "removed_seats", "accessible_seats",
                    )
                },
            )
            row_counts = compute_row_counts(
                section.rows, section.seats_per_row, section.offset_mode, section.alt_row_seat_delta
            )
            generate_seats(
                section,
                row_counts,
                removed_ids={tuple(identity) for identity in section.removed_seats},
                accessible_ids={tuple(identity) for identity in section.accessible_seats},
            )

    return chart


