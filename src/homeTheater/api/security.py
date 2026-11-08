"""Request-level protection: the ``Host`` allowlist and response security headers.

These are pure ASGI middleware. The config is read per request rather than when the
app object is built, because ``homeTheater.api.app`` builds the app at import time,
before a config needs to exist.
"""

from __future__ import annotations

import ipaddress
import os
import socket
from functools import lru_cache

from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ..config import get_config
from ..config.settings import Server
from ..logging_setup import get_logger

log = get_logger(__name__)

EXTRA_HOSTS_ENV = "HOME_THEATER_ALLOWED_HOSTS"

# Tailscale and other CGNAT overlays: not "private" to ipaddress, but just as local.
_CGNAT = ipaddress.ip_network("100.64.0.0/10")

# Inline ``style=`` attributes carry computed bar widths, in the templates and in the
# HTML app.js builds, so ``style-src-attr`` allows those and nothing else: no <style>
# blocks, no inline script. Posters are the only cross-origin resource.
CONTENT_SECURITY_POLICY = "; ".join(
    (
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self'",
        "style-src-attr 'unsafe-inline'",
        "img-src 'self' data: https://image.tmdb.org",
        "connect-src 'self'",
        "object-src 'none'",
        "base-uri 'self'",
        "form-action 'self'",
        "frame-ancestors 'none'",
    )
)
_HEADERS = (
    (b"content-security-policy", CONTENT_SECURITY_POLICY.encode()),
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"same-origin"),
)
# Swagger UI and ReDoc load from a CDN and run inline script. They only exist when
# HOME_THEATER_DOCS is on, and they can't work under the policy above.
_NO_CSP_PATHS = ("/docs", "/redoc")


def _header(scope: Scope, name: bytes) -> str:
    for key, value in scope.get("headers", ()):
        if key == name:
            return str(value.decode("latin-1"))
    return ""


def host_name(header: str) -> str:
    """The lower-cased host from a ``Host`` header: no port, no IPv6 brackets."""

    host = header.strip().lower()
    if host.startswith("["):
        return host[1:].split("]", 1)[0]
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


@lru_cache(maxsize=1)
def machine_names() -> tuple[str, ...]:
    """This machine's name, with and without ``.local``. Lower-cased: browsers lower
    the host, and macOS names usually aren't (``kligvassers-Air``)."""

    name = socket.gethostname().lower()
    bare = name.removesuffix(".local")
    return (bare, f"{bare}.local")


def _is_local_ip(host: str) -> bool:
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    if address.version == 4 and address in _CGNAT:
        return True
    return address.is_private or address.is_loopback or address.is_link_local


def _matches(host: str, pattern: str) -> bool:
    pattern = pattern.strip().lower()
    if pattern == "*":
        return True
    if pattern.startswith("*."):
        return host.endswith(pattern[1:])
    return host == host_name(pattern) or host == pattern


def host_allowed(host: str, server: Server) -> bool:
    if not host:
        return False
    if server.allow_private_ips and _is_local_ip(host):
        return True
    extra = os.environ.get(EXTRA_HOSTS_ENV, "").split(",")
    patterns = [*server.allowed_hosts, *machine_names(), *(p for p in extra if p.strip())]
    return any(_matches(host, p) for p in patterns)


class HostGuardMiddleware:
    """Refuse requests for a ``Host`` this dashboard doesn't go by.

    Not Starlette's ``TrustedHostMiddleware``, which is case-sensitive, mangles
    ``[::1]:8000`` and has no way to say "any LAN address". The rule is the same:
    the check stops DNS rebinding, where a malicious page gets a browser on the
    owner's LAN to talk to this open port under the attacker's domain name.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        header = _header(scope, b"host")
        if host_allowed(host_name(header), get_config().server):
            await self.app(scope, receive, send)
            return
        log.warning("http.host_refused", host=header[:255])
        response = PlainTextResponse(
            "Host not allowed. Add it to server.allowed_hosts in config.yaml.", status_code=400
        )
        await response(scope, receive, send)


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        skip_csp = scope["path"].startswith(_NO_CSP_PATHS)

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", ()))
                present = {key.lower() for key, _ in headers}
                for key, value in _HEADERS:
                    if key in present or (skip_csp and key == b"content-security-policy"):
                        continue
                    headers.append((key, value))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_with_headers)
