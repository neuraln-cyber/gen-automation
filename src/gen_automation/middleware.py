import re
from collections.abc import Awaitable, Callable
from uuid import uuid4

import structlog
from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

from gen_automation.config import Environment, Settings

REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


def content_security_policy(
    environment: Environment,
    *,
    allow_same_origin_scripts: bool = False,
) -> str:
    image_sources = "'self' https:"
    if environment in {Environment.LOCAL, Environment.TEST}:
        image_sources = f"{image_sources} http:"
    script_sources = "'self'" if allow_same_origin_scripts else "'none'"
    return (
        "default-src 'self'; base-uri 'none'; connect-src 'self'; "
        "font-src 'self'; form-action 'self'; frame-ancestors 'none'; "
        f"img-src {image_sources}; object-src 'none'; "
        f"script-src {script_sources}; style-src 'self' 'unsafe-inline'"
    )


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        supplied_request_id = request.headers.get("x-request-id", "")
        request_id = (
            supplied_request_id
            if REQUEST_ID_PATTERN.fullmatch(supplied_request_id)
            else str(uuid4())
        )

        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(
            request_id=request_id,
            method=request.method,
            path=request.url.path,
        )
        request.state.request_id = request_id
        try:
            response = await call_next(request)
        finally:
            structlog.contextvars.clear_contextvars()

        response.headers["X-Request-ID"] = request_id
        response.headers["Cache-Control"] = "no-store"
        response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
        response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
        settings: Settings = request.app.state.settings
        response.headers["Content-Security-Policy"] = content_security_policy(
            settings.environment,
            allow_same_origin_scripts=(
                request.url.path == "/dashboard" or request.url.path.startswith("/dashboard/")
            ),
        )
        response.headers["Permissions-Policy"] = (
            "camera=(), geolocation=(), microphone=(), payment=(), usb=()"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Robots-Tag"] = "noindex, nofollow, noimageindex"
        if settings.environment in {Environment.STAGING, Environment.PRODUCTION}:
            response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
        return response
