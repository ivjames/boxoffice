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
from collections import Counter

from django.conf import settings
from django.db import transaction

from .generation import compute_row_counts, generate_row_labels, generate_seats
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

# Images are downscaled client-side to this long edge before upload. It's
# the vision model's own native maximum (Opus 4.7+ high-res limit) -- the
# API downscales anything bigger server-side anyway, so nothing is lost --
# and it keeps a full-res phone photo well clear of the API's hard
# pixel-dimension cap, which is what a 400 on a big IMG_xxxx.png upload
# turned out to be.
MAX_IMAGE_EDGE_PX = 2576


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
        "row_alignment": {
            "type": "string",
            "enum": ["edge", "center"],
            "description": (
                "How shorter rows sit against the widest one: 'edge' when rows keep one "
                "edge/aisle aligned (typical left/right blocks), 'center' when each row is "
                "centered on the block's axis (typical center blocks / symmetric trapezoids)."
            ),
        },
        "offset_mode": {"type": "string", "enum": ["repeated", "alternating"]},
        "row_x_offset": {"type": "number", "description": "Per-row x stagger in seat widths. 0 for a plain grid with vertical seat columns; see the prompt's offset-detection guidance."},
        "alt_row_seat_delta": {"type": "integer", "description": "alternating mode only: seats added/dropped on every other row. Usually 0."},
        "numbering_scheme": {
            "type": "string",
            "enum": ["sequential", "odd_desc_left", "even_asc_right", "hundreds", "hundreds_flat"],
        },
        "seat_number_base": {
            "type": "integer",
            "description": (
                "Added to every seat number, composing with numbering_scheme. 0 for plain "
                "numbers. Center blocks numbered in the 100s but keeping an odd/even "
                "convention use the odd/even scheme with seat_number_base=100: a row printed "
                "'...119 117 ... 103 101' is odd_desc_left + 100; '102 104 ... 120' is "
                "even_asc_right + 100."
            ),
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
        "seat_pitch", "row_pitch", "arc_radius", "row_alignment", "offset_mode",
        "row_x_offset", "alt_row_seat_delta", "numbering_scheme", "seat_number_base",
        "row_label_scheme", "row_label_start", "removed_seats", "accessible_seats",
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
removed_seats: the highest odd numbers for odd_desc_left, the highest even \
numbers for even_asc_right, the highest numbers for hundreds_flat/sequential \
-- unless the chart clearly shows a mid-row gap, in which case remove exactly \
the printed gap.
- row_alignment: look at where the shorter rows sit. 'edge' when every row \
keeps one edge or aisle aligned (typical left/right blocks -- the removal \
convention above then also produces the right geometry automatically). \
'center' when each row is centered on the block's axis (typical center \
blocks that widen toward the back): STILL remove the highest numbers as \
above -- the importer re-centers the rows itself.
- rows counts every row POSITION front to back, including a row that is \
entirely absent from a section (e.g. a cross-aisle where the center block has \
no row D but the sides do): keep the position and put ALL of that row's seats \
in removed_seats, so the rows behind it keep their correct letters.
- Printed cut-outs -- TECH BOOTH, ADA/wheelchair PLATFORM, sound/mix desk, \
camera positions, any labelled box occupying seat positions -- are seats \
that DO NOT EXIST: put every seat they displace in removed_seats. Count the \
printed seats of each affected row individually; never assume a row matches \
its neighbours.
- Row labels run front to back per row_label_scheme starting at \
row_label_start; seat numbers follow numbering_scheme, so with 'sequential' \
the leftmost seat of every row is "1".
- Curved/fanned rows: set arc_radius (front-row radius, in seat units -- \
gentler curve = larger radius). Straight rows: arc_radius null.
- Offset/stagger detection -- judge by the SEAT COLUMNS, not the row ends: \
vertical columns = row_x_offset 0. Columns slanting steadily sideways going \
back (a parallelogram/raked side block) = offset_mode 'repeated' with \
row_x_offset set to the per-row shift in seat widths (positive shifts later \
rows right; typically 0.3-1.0, use a negative value for a leftward lean). \
Every OTHER row shifted about half a seat (brick/stadium stagger) = \
offset_mode 'alternating' with row_x_offset 0.5 (and alt_row_seat_delta \
+1/-1 if alternating rows are one seat longer/shorter). A whole block \
visibly tilted, rows no longer horizontal = rotation in degrees instead.
- numbering_scheme + seat_number_base: read the printed seat numbers \
CAREFULLY, including their parity. Odd numbers descending toward the aisle \
= odd_desc_left; ascending evens = even_asc_right; rows numbered \
101/201/301 by row = hundreds; every row restarting at 101, 102, 103 = \
hundreds_flat; plain 1,2,3 = sequential. When a block keeps an odd/even \
convention but in a higher band -- '...119 117 ... 103 101' or \
'102 104 ... 120' -- use the odd/even scheme with seat_number_base=100 \
(NOT hundreds_flat: 101 102 103 and 101 103 105 are different rows).
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


def _sniff_media_type(data):
    """The media type the file's MAGIC BYTES claim, or None if unrecognised.
    Browsers/filenames lie routinely -- a renamed iPhone photo arrives as
    'IMG_0128.png' with image/png while the bytes are JPEG or HEIC, and the
    API 400s on the mismatch -- so the bytes win over the claimed type."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data.startswith(b"%PDF-"):
        return "application/pdf"
    if len(data) > 12 and data[4:8] == b"ftyp":
        # The ISO base-media container: HEIC/HEIF/AVIF photos (iPhone default).
        return "image/heic"
    return None


def _prepare_upload(data, media_type):
    """Make the upload API-safe before it costs tokens: trust the magic
    bytes over the claimed media type, reject HEIC with an actionable
    message, and normalise real images (EXIF rotation applied, long edge
    capped at MAX_IMAGE_EDGE_PX). Bytes we can't identify pass through
    unchanged -- if they're genuinely bad the API's own error is surfaced
    verbatim by _request_spec."""
    sniffed = _sniff_media_type(data)
    if sniffed == "image/heic":
        raise ChartParsingError(
            "This looks like an iPhone HEIC/HEIF photo, which the parser can't read -- "
            "export or share it as JPEG or PNG and upload that instead."
        )
    if sniffed and sniffed != media_type:
        media_type = sniffed
    if sniffed in ("image/png", "image/jpeg", "image/gif", "image/webp"):
        data, media_type = _normalise_image(data, media_type)
    return data, media_type


def _normalise_image(data, media_type):
    """Decode a real image with Pillow (already a project dependency), apply
    its EXIF orientation (a sideways phone photo parses sideways otherwise),
    and downscale anything whose long edge exceeds MAX_IMAGE_EDGE_PX.
    Untouched PNG/JPEG bytes are returned as-is -- no pointless re-encode;
    GIF/WebP are converted to PNG so the API sees a boring, safe format."""
    import io

    from PIL import Image, ImageOps

    try:
        image = Image.open(io.BytesIO(data))
        image.load()
    except Exception as exc:
        raise ChartParsingError(
            "Couldn't read that image file -- it may be corrupted or truncated. "
            "Re-export it and try again."
        ) from exc

    image = ImageOps.exif_transpose(image)
    resized = False
    long_edge = max(image.size)
    if long_edge > MAX_IMAGE_EDGE_PX:
        scale = MAX_IMAGE_EDGE_PX / long_edge
        image = image.resize(
            (max(1, round(image.width * scale)), max(1, round(image.height * scale))),
            Image.LANCZOS,
        )
        resized = True

    if not resized and media_type in ("image/png", "image/jpeg"):
        return data, media_type

    buffer = io.BytesIO()
    if media_type == "image/jpeg":
        image.convert("RGB").save(buffer, "JPEG", quality=90)
    else:
        if image.mode not in ("RGB", "RGBA"):
            image = image.convert("RGBA")
        image.save(buffer, "PNG")
        media_type = "image/png"
    return buffer.getvalue(), media_type


def _content_block(data_b64, media_type):
    """The API content block for the uploaded file: PDFs go in a `document`
    block, images in an `image` block -- both base64, both natively
    supported by the vision models."""
    source = {"type": "base64", "media_type": media_type, "data": data_b64}
    if media_type == "application/pdf":
        return {"type": "document", "source": source}
    return {"type": "image", "source": source}


_VERIFY_PROMPT_TEMPLATE = """\
A first pass extracted the JSON spec below from this seating chart. \
Re-examine the chart and correct that extraction, then return the complete \
corrected spec (identical if nothing is wrong). Check, section by section:
- Recount each row's printed seats. Wherever the printed count disagrees \
with what the spec implies (seats_per_row minus that row's removed_seats), \
fix removed_seats. Look ESPECIALLY for printed cut-outs -- TECH BOOTH, ADA/\
wheelchair platforms, sound/mix desks, camera positions -- every seat they \
displace must be in removed_seats.
- Reproduce each row's printed numbers from numbering_scheme + \
seat_number_base and compare against the chart, INCLUDING PARITY: \
'101 102 103' is hundreds_flat; '...105 103 101' is odd_desc_left with \
seat_number_base=100; '102 104 106...' is even_asc_right with \
seat_number_base=100. These are different numbering systems -- do not \
conflate them.
- Check row labels against the letters printed beside each row: skipped \
letters (I/O), and row_label_start for any section whose first row isn't A.
- Check every wheelchair/accessible marking is in accessible_seats.

First-pass extraction:
{spec_json}
"""


def _request_spec(client, model, file_block, prompt_text):
    """One schema-constrained vision request: send `file_block` + \
    `prompt_text`, return the raw (not yet validated) spec dict. All API
    failure modes surface as ChartParsingError."""
    import anthropic

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
                        file_block,
                        {"type": "text", "text": prompt_text},
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
        # Surface the API's own explanation -- a bare "(400)" told staff
        # nothing when a bad upload (oversized/mistyped image) was rejected.
        detail = (getattr(exc, "message", "") or str(exc)).strip()
        raise ChartParsingError(
            f"The parsing service rejected the request ({exc.status_code}): {detail[:300]}"
        ) from exc
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
    return spec, response


def parse_chart_file(data, media_type, *, verify=True, on_progress=None):
    """Send the file bytes to the Claude API and return a validated chart
    spec dict (see CHART_SPEC_SCHEMA / validate_chart_spec) with the API's
    token accounting attached as spec["usage"] (summed across passes -- see
    _usage_dict / describe_usage). Raises ChartParsingError for every
    failure mode -- unsupported type, oversize file, API errors, refusals,
    truncated output.

    `verify` (default on) runs a SECOND pass: the image goes back to the
    model together with the validated first-pass spec and row-by-row
    checking instructions (_VERIFY_PROMPT_TEMPLATE). Extraction recall --
    missed cut-outs, mistaken numbering parity -- is the failure mode
    observed in live parses, and a self-check against its own output is the
    cheapest effective counter. Costs a second model call (roughly doubles
    tokens); disable for quick/cheap runs via the management command's
    --no-verify.

    `on_progress`, if given, is called with a short stage string
    ("extracting", "verifying") before each model call -- the background
    job worker (run_parse_job) uses it to surface progress to the
    dashboard's polling UI."""
    import base64

    if media_type not in SUPPORTED_MEDIA_TYPES:
        raise ChartParsingError(
            "Unsupported file type. Upload a PNG, JPEG, GIF, WebP image or a PDF."
        )
    if len(data) > MAX_UPLOAD_BYTES:
        raise ChartParsingError("File is too large (20 MB max).")
    if not data:
        raise ChartParsingError("The uploaded file is empty.")

    data, media_type = _prepare_upload(data, media_type)

    client = _get_client()
    model = getattr(settings, "CHART_PARSING_MODEL", "claude-opus-4-8")
    file_block = _content_block(base64.standard_b64encode(data).decode("ascii"), media_type)

    if on_progress:
        on_progress("extracting")
    raw_spec, response = _request_spec(client, model, file_block, _PARSE_PROMPT)
    responses = [response]

    if verify:
        first_pass = validate_chart_spec(raw_spec)
        if on_progress:
            on_progress("verifying")
        raw_spec, second_response = _request_spec(
            client,
            model,
            file_block,
            _VERIFY_PROMPT_TEMPLATE.format(spec_json=json.dumps(first_pass, indent=2)),
        )
        responses.append(second_response)

    spec = validate_chart_spec(raw_spec)
    spec["usage"] = _usage_dict(model, responses)
    logger.info(
        "Parsed seating chart (%s, %s pass(es)): %s section(s), input_tokens=%s "
        "output_tokens=%s cache_read=%s cache_creation=%s",
        model,
        len(responses),
        len(spec["sections"]),
        spec["usage"].get("input_tokens"),
        spec["usage"].get("output_tokens"),
        spec["usage"].get("cache_read_input_tokens"),
        spec["usage"].get("cache_creation_input_tokens"),
    )
    return spec


def _usage_dict(model, responses):
    """The API's token accounting for one parse (summed over its passes),
    as a plain dict: {"model", "input_tokens", "output_tokens",
    "cache_read_input_tokens", "cache_creation_input_tokens"} -- fields
    absent from every response stay None so callers can render "unknown"
    honestly rather than a fake 0. Attached to the spec as spec["usage"]
    (validate_chart_spec ignores unknown keys, so a spec with usage still
    round-trips through build_chart_from_spec)."""
    fields = (
        "input_tokens",
        "output_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
    )
    totals = {field: None for field in fields}
    for response in responses:
        usage = getattr(response, "usage", None)
        for field in fields:
            value = getattr(usage, field, None)
            if value is not None:
                totals[field] = (totals[field] or 0) + value
    return {"model": model, **totals}


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


# _derived_center_offset only trusts its linear fit when the row widths
# actually follow it: at least this fraction of occupied rows must sit
# within the tolerance (in seats) of the fitted taper. One weird row (a
# mix-desk cut-out) is tolerated; an erratic distribution isn't -- better
# to leave a centered block visibly un-centered for staff to fix than to
# apply an offset that merely LOOKS deliberate.
_CENTER_FIT_TOLERANCE_SEATS = 1.25
_CENTER_FIT_MIN_FRACTION = 0.75


def _derived_center_offset(section):
    """The row_x_offset (REPEATED mode) that approximately re-CENTERS a
    section's ragged rows, derived from the per-row widths its removed_seats
    imply. Removing the highest printed numbers trims grid positions from
    one end, which leaves rows edge-aligned -- right for aisle-hugging side
    blocks, wrong for a symmetric center block that widens toward the back.
    Shifting each row by -seat_pitch/2 per extra seat re-centers it; since
    row_x_offset is one linear term for the whole section, the slope is
    anchored on the front row vs the WIDEST row (robust against a single
    odd back row, e.g. a mix-desk cut-out) with a last-row fallback when the
    front row IS the widest.

    Conservative by design: a linear offset can only center a steady taper,
    so when the width distribution doesn't fit the implied line (see
    _CENTER_FIT_* above) -- oscillating widths, several irregular rows --
    this returns 0.0 and attempts nothing, leaving the block edge-aligned
    for staff to shape in the editor. Same for ALTERNATING offset_mode,
    whose stagger is its own mechanism."""
    if section["offset_mode"] == Section.OffsetMode.ALTERNATING:
        return 0.0
    labels = generate_row_labels(
        section["rows"], section["row_label_scheme"], section["row_label_start"]
    )
    removed_per_label = Counter(label for label, _ in section["removed_seats"])
    widths = [
        max(0, section["seats_per_row"] - removed_per_label.get(label, 0)) for label in labels
    ]
    occupied = [(index, width) for index, width in enumerate(widths) if width > 0]
    if len(occupied) < 2:
        return 0.0
    first_index, first_width = occupied[0]
    anchor_index, anchor_width = max(occupied, key=lambda pair: (pair[1], pair[0]))
    if anchor_index == first_index:  # front row is the widest -- narrowing block
        anchor_index, anchor_width = occupied[-1]
    slope = (anchor_width - first_width) / (anchor_index - first_index)

    fitting = sum(
        1
        for index, width in occupied
        if abs(width - (first_width + slope * (index - first_index)))
        <= _CENTER_FIT_TOLERANCE_SEATS
    )
    if fitting < _CENTER_FIT_MIN_FRACTION * len(occupied):
        return 0.0

    # Same +/-2 cap the editor's slider and chart_editor_save enforce.
    return max(-2.0, min(2.0, -section["seat_pitch"] * slope / 2.0))


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
        alignment = raw.get("row_alignment")
        section = (
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
                "seat_number_base": _clamp_int(raw.get("seat_number_base"), 0, 900, 0),
                "row_label_scheme": row_labels if row_labels in row_label_values else Section.RowLabelScheme.SKIP_IO,
                "row_label_start": _clamp_int(raw.get("row_label_start"), 0, 2 * MAX_ROWS, 0),
                "removed_seats": _seat_identity_list(raw.get("removed_seats")),
                "accessible_seats": _seat_identity_list(raw.get("accessible_seats")),
            }
        )
        section["row_alignment"] = alignment if alignment in ("edge", "center") else "edge"
        # Fold "center" alignment into a concrete row_x_offset, but never
        # override an offset the parse set explicitly (a centered AND raked
        # block). Idempotent: re-validating a folded spec sees a non-zero
        # offset and leaves it alone.
        if section["row_alignment"] == "center" and not section["row_x_offset"]:
            section["row_x_offset"] = _derived_center_offset(section)
        sections.append(section)

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
                        "seat_number_base", "row_label_scheme", "row_label_start",
                        "removed_seats", "accessible_seats",
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



# --- background jobs (ChartParseJob -> run_chart_parse worker) --------------


def run_parse_job(job_id):
    """Execute one ChartParseJob synchronously: claim it (PENDING ->
    RUNNING, atomically -- a double-spawned worker exits instead of running
    the parse twice), stream progress onto the row, parse, build (into
    `replace_chart` in place, or as a new chart), and record the outcome.
    Never raises: every failure -- expected (ChartParsingError, surfaced
    verbatim, it's staff-safe by contract) or not (logged, generic message)
    -- lands in status=FAILED with `error` set, because the caller is a
    detached worker process nobody's watching. Returns the refreshed job,
    or None if it wasn't claimable."""
    from django.utils import timezone

    from .models import ChartParseJob

    claimed = ChartParseJob.objects.filter(
        pk=job_id, status=ChartParseJob.Status.PENDING
    ).update(status=ChartParseJob.Status.RUNNING, started_at=timezone.now())
    if not claimed:
        return None
    job = ChartParseJob.objects.get(pk=job_id)

    def on_progress(stage):
        job.progress = stage
        job.save(update_fields=["progress"])

    try:
        with job.upload.open("rb") as f:
            data = f.read()
        spec = parse_chart_file(data, job.media_type, on_progress=on_progress)
        on_progress("building")
        if job.replace_chart_id:
            chart = build_chart_from_spec(
                job.venue, spec, name=job.replace_chart.name, replace=True
            )
        else:
            chart = build_chart_from_spec(job.venue, spec, name=job.chart_name or None)
        job.chart = chart
        job.usage = spec.get("usage") or {}
        job.status = ChartParseJob.Status.SUCCEEDED
        job.error = ""
    except ChartParsingError as exc:
        job.status = ChartParseJob.Status.FAILED
        job.error = str(exc)
    except Exception:
        logger.exception("Chart parse job %s crashed", job_id)
        job.status = ChartParseJob.Status.FAILED
        job.error = "Unexpected error while parsing -- see the server log."
    job.progress = ""
    job.finished_at = timezone.now()
    job.save()
    return job


def spawn_parse_job(job):
    """Launch the `run_chart_parse` management command for `job` as a
    DETACHED subprocess (own session, no inherited stdio) so the parse's
    multi-minute vision calls run outside any web worker's request/timeout
    lifecycle -- the right-sized async worker for this deliberately
    celery-free stack. The subprocess inherits os.environ, which carries
    DJANGO_SETTINGS_MODULE (django-environ's read_env loads .env into the
    process environment at settings import). Isolated so tests can patch it."""
    import subprocess
    import sys

    subprocess.Popen(
        [sys.executable, str(settings.BASE_DIR / "manage.py"), "run_chart_parse", str(job.pk)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
        cwd=str(settings.BASE_DIR),
    )
