class NoCacheHTMLMiddleware:
    """Stop browsers reusing a stale HTML page without revalidating.

    Static assets are content-hashed by WhiteNoise's manifest storage (see
    STORAGES in config/settings/base.py) and are safe to cache forever -- the
    URL changes when the bytes change. But the HTML that *references* those
    hashed URLs is dynamic, and Safari in particular will heuristically cache
    an HTML response that carries no Cache-Control, then serve the STALE copy
    on refresh -- so a fresh deploy (new asset hashes, new seat availability)
    looks like it "didn't take", the exact "I refresh in Safari and don't see
    updates" report.

    Marking HTML `no-store` forces a re-fetch on every load, so the page always
    pulls the current hashed asset URLs (and current availability). Only touches
    text/html; leaves WhiteNoise's own static responses alone (those short-
    circuit above this middleware) and never overrides a Cache-Control a view
    set on purpose -- @never_cache, or a view that deliberately opts into
    caching.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        content_type = response.get("Content-Type", "")
        if content_type.startswith("text/html") and not response.has_header("Cache-Control"):
            response["Cache-Control"] = "no-cache, no-store, must-revalidate"
            response["Pragma"] = "no-cache"
        return response
