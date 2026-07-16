"""SSRF-guarded outbound fetch. Every URL the derive agent touches -- the page
itself, each redirect hop, and each linked stylesheet -- is validated here
before a byte of it is requested."""

import ipaddress
import socket
from urllib.parse import urljoin, urlparse

from .scoring import ColorDeriveError

MAX_FETCH_BYTES = 2 * 1024 * 1024
FETCH_TIMEOUT = 10

# Cap on redirect hops we'll follow -- each is re-validated by _guard_public_url.
MAX_REDIRECTS = 5


def _guard_public_url(url):
    """Raise ColorDeriveError unless `url` is an http(s) URL whose host resolves
    ENTIRELY to public IP addresses. Blocks the SSRF/port-scan surface a
    manager-supplied URL would otherwise open: loopback (localhost), private
    ranges, link-local (incl. the 169.254.169.254 cloud-metadata endpoint),
    and other reserved/multicast space. Applied to the page URL, every redirect
    hop, and every linked stylesheet."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ColorDeriveError("Only http(s) web addresses can be read.")
    host = parsed.hostname
    if not host:
        raise ColorDeriveError("That address has no host to read.")
    try:
        infos = socket.getaddrinfo(host, parsed.port or 80, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise ColorDeriveError(f"Couldn't resolve {host}.") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global or ip.is_multicast:
            raise ColorDeriveError(
                "That address points at a private or internal host, which can't be read."
            )


def _is_public_url(url):
    """Boolean form of _guard_public_url -- for the screenshot render, where a
    raise would be noise (a blocked sub-resource is just skipped, not fatal)."""
    try:
        _guard_public_url(url)
        return True
    except ColorDeriveError:
        return False


def _http_fetch(url):
    """Fetch a URL's text via `requests`, capped at MAX_FETCH_BYTES. Every URL
    (the page, each redirect hop, and each linked stylesheet -- all arrive
    here) is SSRF-guarded first (_guard_public_url). Redirects are followed
    manually so each Location is re-validated before it's requested. Outbound
    goes through the environment's configured proxy (requests honors
    HTTPS_PROXY)."""
    import requests

    headers = {"User-Agent": "boxo.show color-scheme agent"}
    for _hop in range(MAX_REDIRECTS + 1):
        _guard_public_url(url)
        resp = requests.get(
            url, timeout=FETCH_TIMEOUT, headers=headers, stream=True, allow_redirects=False
        )
        if resp.is_redirect and resp.headers.get("Location"):
            url = urljoin(url, resp.headers["Location"])
            resp.close()
            continue
        resp.raise_for_status()
        content = resp.raw.read(MAX_FETCH_BYTES, decode_content=True) or b""
        return content.decode(resp.encoding or "utf-8", errors="replace")
    raise ColorDeriveError("Too many redirects.")
