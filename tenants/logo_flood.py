"""Dependency-light background removal for a logo on a clean, solid background
-- the common case: a wordmark or character exported on white (or any single
colour). It removes the background-coloured region *connected to the image edge*
and, crucially, leaves interior areas of that same colour alone, because they're
enclosed by the artwork and not connected to the edge.

That interior distinction is exactly where the ML model (tenants/logo_bg.py)
over-removes -- a white belly/legs on a white background go semi-transparent,
because the model can't tell white subject from white background. Flood-fill
can, thanks to the artwork's own outline blocking the fill.

Pillow-only (no numpy / rembg), so it runs instantly with no model, cold start,
or extra dependency. Returns None when the border ISN'T a uniform colour (a logo
sitting on a photo or gradient -- no clean background to key on), so the caller
falls back to the ML path.
"""

import io

# The image counts as "clean background" only if at least this fraction of its
# border samples fall within FLOOD_BORDER_TOLERANCE of the border's median
# colour. Below it the edge is busy (photo/gradient) -> no clean key -> None.
FLOOD_BORDER_UNIFORM_MIN = 0.85
FLOOD_BORDER_TOLERANCE = 32
# How close a pixel must be to the background colour to count as background
# during the flood -- generous enough to swallow anti-aliased edge pixels.
FLOOD_BG_TOLERANCE = 40


def _channel_distance(a, b):
    return max(abs(a[0] - b[0]), abs(a[1] - b[1]), abs(a[2] - b[2]))


def flood_fill_background(raw):
    """Return PNG bytes with the solid background made transparent, or None if
    the image can't be read or has no uniform background to key on. Bytes ->
    bytes so the caller treats the result like any other cutout."""
    from PIL import Image, ImageChops, ImageDraw, ImageFilter

    try:
        image = Image.open(io.BytesIO(raw))
        image.load()
    except Exception:
        return None

    rgb = image.convert("RGB")
    w, h = rgb.size
    if w < 3 or h < 3:
        return None

    # Sample the border to find the background colour and gauge how uniform it is.
    step = max(1, min(w, h) // 64)
    xs, ys = range(0, w, step), range(0, h, step)
    border_pts = (
        [(x, 0) for x in xs] + [(x, h - 1) for x in xs]
        + [(0, y) for y in ys] + [(w - 1, y) for y in ys]
    )
    border = [rgb.getpixel(p) for p in border_pts]
    median = tuple(sorted(c[i] for c in border)[len(border) // 2] for i in range(3))

    uniform = sum(1 for c in border if _channel_distance(c, median) <= FLOOD_BORDER_TOLERANCE)
    if uniform / len(border) < FLOOD_BORDER_UNIFORM_MIN:
        return None  # busy border -> no clean background -> let the ML path try

    # Binary "looks like the background colour" map (255 = background-coloured).
    solid = Image.new("RGB", (w, h), median)
    d_r, d_g, d_b = ImageChops.difference(rgb, solid).split()
    max_diff = ImageChops.lighter(ImageChops.lighter(d_r, d_g), d_b)
    bgsim = max_diff.point(lambda v: 255 if v <= FLOOD_BG_TOLERANCE else 0)

    # Flood from every background-coloured border pixel: the region connected to
    # the edge becomes 128 (true background); interior background-coloured areas
    # stay 255 (part of the logo -> kept opaque).
    load = bgsim.load()
    for sx, sy in border_pts:
        if load[sx, sy] == 255:
            ImageDraw.floodfill(bgsim, (sx, sy), 128, thresh=0)

    if bgsim.histogram()[128] == 0:
        return None  # nothing keyed off the edge -> not actually a clean background

    alpha = bgsim.point(lambda v: 0 if v == 128 else 255)
    alpha = alpha.filter(ImageFilter.GaussianBlur(0.6))  # soften the cut edge

    out = image.convert("RGBA")
    out.putalpha(alpha)
    buf = io.BytesIO()
    out.save(buf, format="PNG")
    return buf.getvalue()
