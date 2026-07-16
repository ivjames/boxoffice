"""Render a homepage to a screenshot with headless Chromium (the vision path).
Best-effort and SSRF-guarded like the plain fetch: the page URL and every
sub-resource request must resolve to a public host, or the render aborts."""

import logging
import os
from urllib.parse import urlparse

from ._netfetch import _is_public_url

logger = logging.getLogger(__name__)

# Homepage screenshot render (the vision path). Above-the-fold viewport -- the
# header, logo, hero, and primary buttons that carry the brand live there, and a
# capped viewport bounds both render time and the image tokens Claude sees.
RENDER_VIEWPORT = {"width": 1280, "height": 1600}
RENDER_TIMEOUT_MS = 20000
RENDER_MAX_EDGE_PX = 1400

# Injected before any page script during a render: removes the connection APIs
# that bypass request-route interception (WebSocket, WebRTC), so a hostile
# homepage can't reach a private host through them. Defined non-configurable so
# page code can't restore the originals.
_RENDER_BLOCK_SOCKETS_JS = """
(() => {
  const block = (name) => {
    try { Object.defineProperty(window, name, {value: undefined, configurable: false, writable: false}); }
    catch (e) { /* already locked -- fine */ }
  };
  ['WebSocket', 'RTCPeerConnection', 'webkitRTCPeerConnection', 'EventSource'].forEach(block);
})();
"""


def _find_chromium_executable():
    """The Chromium binary under PLAYWRIGHT_BROWSERS_PATH, if the environment
    pins one whose build differs from the pip package's expected revision (so
    Playwright's own resolution would miss it). None lets Playwright resolve
    its default -- the normal case on a machine where `playwright install`
    matched the package."""
    base = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if not base:
        return None
    import glob

    for pattern in (
        "chromium-*/chrome-linux/chrome",
        "chromium_headless_shell-*/chrome-linux/headless_shell",
    ):
        hits = sorted(glob.glob(os.path.join(base, pattern)))
        if hits:
            return hits[-1]
    link = os.path.join(base, "chromium")
    return link if os.path.exists(link) else None


def _downscale_png(png_bytes):
    """Cap the screenshot's long edge at RENDER_MAX_EDGE_PX so the image block
    stays cheap in tokens. Pillow is already a project dependency; on any
    decode trouble the original bytes pass through."""
    try:
        import io

        from PIL import Image

        image = Image.open(io.BytesIO(png_bytes))
        image.load()
        long_edge = max(image.size)
        if long_edge <= RENDER_MAX_EDGE_PX:
            return png_bytes
        scale = RENDER_MAX_EDGE_PX / long_edge
        image = image.resize(
            (max(1, round(image.width * scale)), max(1, round(image.height * scale))),
            Image.LANCZOS,
        )
        buffer = io.BytesIO()
        image.convert("RGB").save(buffer, "PNG")
        return buffer.getvalue()
    except Exception:  # noqa: BLE001 -- downscale is an optimization, not a gate
        return png_bytes


def render_homepage_png(url):
    """Render `url`'s above-the-fold homepage with headless Chromium and return
    a PNG (downscaled), or None if rendering isn't possible (no Playwright, no
    browser, navigation/timeout failure) -- the caller then refines from text.

    SSRF-guarded like the fetch: the page URL must resolve public, and every
    sub-resource request is re-checked and aborted if it points anywhere
    private (a browser would otherwise reach internal hosts the fetch can't).
    Honors HTTPS_PROXY when the environment sets one (unset in normal prod).
    `ignore_https_errors` is on deliberately: we're capturing pixels off a
    manager-supplied public URL to read colors, not transacting -- a cert
    hiccup shouldn't block branding, and the host is already IP-guarded."""
    if not _is_public_url(url):
        return None
    try:
        from playwright.sync_api import sync_playwright
    except Exception:  # noqa: BLE001 -- browser automation is an optional extra
        logger.info("Playwright not installed; deriving colors without a screenshot")
        return None

    def _guard_route(route):
        req_url = route.request.url
        scheme = urlparse(req_url).scheme.lower()
        if scheme in ("data", "blob", "about"):
            route.continue_()
        elif scheme in ("http", "https") and _is_public_url(req_url):
            route.continue_()
        else:
            route.abort()

    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    png = None
    try:
        with sync_playwright() as p:
            launch = {"headless": True, "args": ["--no-sandbox", "--disable-dev-shm-usage"]}
            executable = _find_chromium_executable()
            if executable:
                launch["executable_path"] = executable
            if proxy:
                launch["proxy"] = {"server": proxy}
            browser = p.chromium.launch(**launch)
            try:
                context_ = browser.new_context(
                    viewport=RENDER_VIEWPORT,
                    ignore_https_errors=True,
                    user_agent="boxo.show color-scheme agent",
                )
                context_.route("**/*", _guard_route)
                # route() guards HTTP(S) sub-resources but NOT WebSocket or
                # WebRTC connections -- a hostile page's script could otherwise
                # open ws:// / RTCPeerConnection straight to a private host,
                # around the guard. Neutralize both APIs before any page script
                # runs (init scripts execute first in every frame). We only need
                # a static paint for colors, so nothing of value is lost.
                context_.add_init_script(_RENDER_BLOCK_SOCKETS_JS)
                page = context_.new_page()
                page.goto(url, wait_until="domcontentloaded", timeout=RENDER_TIMEOUT_MS)
                try:
                    page.wait_for_load_state("networkidle", timeout=5000)
                except Exception:  # noqa: BLE001 -- idle is a nicety, not required
                    pass
                page.wait_for_timeout(800)
                png = page.screenshot(full_page=False)
            finally:
                browser.close()
    except Exception:  # noqa: BLE001 -- any render failure falls back to text
        logger.warning("Homepage render failed for %s", url, exc_info=True)
        return None
    return _downscale_png(png) if png else None
