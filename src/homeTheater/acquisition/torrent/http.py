"""HTML fetch for scraping sources, optionally via a FlareSolverr proxy.

Cloudflare-walled sites (1337x) return a JS challenge to a plain GET. FlareSolverr
is an operator-run headless-browser proxy that solves the challenge and returns
the final HTML — the same mechanism Prowlarr uses. We don't bundle or implement
any bypass; if no proxy is configured we fetch directly and let the caller skip
the source when it gets a challenge page instead of real results.
"""

from __future__ import annotations

import httpx

from ...logging_setup import get_logger

log = get_logger(__name__)

# Text that appears on a Cloudflare interstitial but not on a real results page.
_CHALLENGE_MARKERS = ("Just a moment", "cf-browser-verification", "challenge-platform")


class ChallengeError(RuntimeError):
    """The response was a Cloudflare challenge, not the page we asked for."""


async def fetch_html(
    client: httpx.AsyncClient,
    url: str,
    *,
    flaresolverr_url: str | None,
    timeout: float,
) -> str:
    """Return the HTML at ``url``. Routes through FlareSolverr when configured.

    Raises :class:`ChallengeError` if a direct fetch hits a Cloudflare wall so the
    caller can degrade gracefully (log + skip the source).
    """

    if flaresolverr_url:
        return await _via_flaresolverr(client, url, flaresolverr_url, timeout)

    resp = await client.get(url, timeout=timeout, follow_redirects=True)
    resp.raise_for_status()
    html = resp.text
    if any(marker in html for marker in _CHALLENGE_MARKERS):
        raise ChallengeError(
            f"{url} is behind a Cloudflare challenge; set torrent.flaresolverr_url"
        )
    return html


async def _via_flaresolverr(
    client: httpx.AsyncClient, url: str, proxy_url: str, timeout: float
) -> str:
    endpoint = f"{proxy_url.rstrip('/')}/v1"
    # FlareSolverr drives a real browser; give it a longer budget than the outer
    # per-request timeout, which would otherwise abort a legitimate solve.
    payload = {"cmd": "request.get", "url": url, "maxTimeout": 60_000}
    resp = await client.post(endpoint, json=payload, timeout=max(timeout, 70.0))
    resp.raise_for_status()
    data = resp.json()
    solution = data.get("solution") or {}
    html = solution.get("response")
    if not isinstance(html, str) or not html:
        raise ChallengeError(f"FlareSolverr returned no HTML for {url}: {data.get('message')}")
    return html
