"""Tests for NoCacheHTMLMiddleware -- the fix for "I refresh in Safari and
don't see updates" (a stale HTML page still pointing at old asset hashes)."""

from django.http import HttpResponse, JsonResponse

from config.middleware import NoCacheHTMLMiddleware


def _run(response):
    return NoCacheHTMLMiddleware(lambda request: response)(request=None)


def test_html_response_is_marked_no_store():
    result = _run(HttpResponse("<html></html>"))  # defaults to text/html
    assert "no-store" in result["Cache-Control"]
    assert result["Pragma"] == "no-cache"


def test_non_html_response_untouched():
    result = _run(JsonResponse({"ok": True}))
    assert not result.has_header("Cache-Control")


def test_view_set_cache_control_is_preserved():
    response = HttpResponse("<html></html>")
    response["Cache-Control"] = "max-age=60"
    result = _run(response)
    assert result["Cache-Control"] == "max-age=60"
    assert not result.has_header("Pragma")
