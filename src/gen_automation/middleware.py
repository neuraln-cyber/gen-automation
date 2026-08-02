import re
from collections.abc import Awaitable, Callable
from urllib.parse import urlsplit
from uuid import uuid4

import structlog
from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

from gen_automation.config import Environment, Settings

REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
CSP_CONNECT_SOURCE_PATTERN = re.compile(r"^https?://[A-Za-z0-9.-]+(?::[0-9]{1,5})?$")


def asset_connection_source(settings: Settings) -> str | None:
    if not settings.storage_enabled:
        return None
    if settings.storage_endpoint_url is not None:
        endpoint = urlsplit(str(settings.storage_endpoint_url))
        source = f"{endpoint.scheme}://{endpoint.netloc}"
        return source if CSP_CONNECT_SOURCE_PATTERN.fullmatch(source) else None
    if not settings.storage_bucket:
        return None
    region = settings.storage_region
    aws_suffix = "amazonaws.com.cn" if region.startswith("cn-") else "amazonaws.com"
    host = (
        f"{settings.storage_bucket}.s3.{aws_suffix}"
        if region == "us-east-1"
        else f"{settings.storage_bucket}.s3.{region}.{aws_suffix}"
    )
    source = f"https://{host}"
    return source if CSP_CONNECT_SOURCE_PATTERN.fullmatch(source) else None


def content_security_policy(
    environment: Environment,
    *,
    allow_same_origin_scripts: bool = False,
    asset_connect_source: str | None = None,
) -> str:
    image_sources = "'self' https:"
    connect_sources = "'self'"
    if environment in {Environment.LOCAL, Environment.TEST}:
        image_sources = f"{image_sources} http:"
    if asset_connect_source is not None:
        if CSP_CONNECT_SOURCE_PATTERN.fullmatch(asset_connect_source) is None:
            raise ValueError("invalid CSP asset connection source")
        connect_sources = f"{connect_sources} {asset_connect_source}"
    script_sources = "'self'" if allow_same_origin_scripts else "'none'"
    return (
        f"default-src 'self'; base-uri 'none'; connect-src {connect_sources}; "
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
            asset_connect_source=(
                asset_connection_source(settings)
                if request.url.path == "/dashboard" or request.url.path.startswith("/dashboard/")
                else None
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
