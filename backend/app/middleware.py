"""Pure-ASGI middleware: security headers and request-size limiting.

Pure ASGI (not BaseHTTPMiddleware) so streaming SSE responses are never buffered.
"""
import json

from .config import settings

_SECURITY_HEADERS = [
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"no-referrer"),
    (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
    (b"cross-origin-resource-policy", b"same-site"),
    # This service only returns JSON / event streams, never documents.
    (b"content-security-policy", b"default-src 'none'; frame-ancestors 'none'"),
]


class SecurityHeadersMiddleware:
    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                present = {k.lower() for k, _ in headers}
                for k, v in _SECURITY_HEADERS:
                    if k not in present:
                        headers.append((k, v))
                if scope.get("path", "").startswith("/api/") and b"cache-control" not in present:
                    headers.append((b"cache-control", b"no-store"))
                if settings.enable_hsts:
                    headers.append((b"strict-transport-security", b"max-age=31536000; includeSubDomains"))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_wrapper)


class _TooLarge(Exception):
    pass


class BodySizeLimitMiddleware:
    """Reject request bodies above `max_bytes` (declared or streamed) with 413."""

    def __init__(self, app, max_bytes: int | None = None) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        limit = self.max_bytes if self.max_bytes is not None else settings.max_request_bytes

        for k, v in scope.get("headers", []):
            if k == b"content-length":
                try:
                    if int(v) > limit:
                        return await self._reject(send)
                except ValueError:
                    return await self._reject(send)

        received = 0
        too_large = False

        async def limited_receive():
            nonlocal received, too_large
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    too_large = True
                    raise _TooLarge()
            return message

        async def tracking_send(message):
            # Frameworks catch the exception above and answer 400; once the limit has
            # been exceeded we drop that response and send our own 413 instead.
            if too_large:
                return
            await send(message)

        try:
            await self.app(scope, limited_receive, tracking_send)
        except _TooLarge:
            pass
        if too_large:
            await self._reject(send)

    @staticmethod
    async def _reject(send):
        body = json.dumps({"detail": "Request body too large"}).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())],
            }
        )
        await send({"type": "http.response.body", "body": body})


_UNSAFE = {b"POST", b"PUT", b"PATCH", b"DELETE"}


class OriginCheckMiddleware:
    """Reject cross-origin state-changing requests to /api/ (defence in depth for cookie auth; protects login too).

    * `Origin` present: must exactly equal one of CORS_ALLOWED_ORIGINS ("null" is rejected).
    * `Origin` absent but `Sec-Fetch-Site` present: must be same-origin / same-site / none.
    * Neither header (curl, server-to-server, tests): allowed; such clients cannot be driven by a victim's browser.
    SameSite cookies cannot separate two ports on the same host, so this check carries that case.
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("method", "").encode() not in _UNSAFE or not scope.get("path", "").startswith("/api/"):
            return await self.app(scope, receive, send)

        headers = {k: v for k, v in scope.get("headers", [])}
        origin = headers.get(b"origin")
        site = headers.get(b"sec-fetch-site")
        ok = True
        if origin is not None:
            ok = origin.decode("latin-1") in settings.cors_origins
        elif site is not None:
            ok = site.decode("latin-1").lower() in ("same-origin", "same-site", "none")
        if not ok:
            body = json.dumps({"detail": "Cross-origin request rejected"}).encode()
            await send({"type": "http.response.start", "status": 403,
                        "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
            await send({"type": "http.response.body", "body": body})
            return
        await self.app(scope, receive, send)
