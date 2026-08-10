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
DASHBOARD_PREVIEW_PATH_PATTERN = re.compile(
    r"^/dashboard/assets/"
    r"[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}/"
    r"previews/dashboard-preview-v[0-9]+/[0-9a-f]{16}\.jpg$",
    re.IGNORECASE,
)


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


def model_artifact_connection_source(settings: Settings) -> str | None:
    bucket_setting = settings.salad_worker_artifact_bucket
    region_setting = settings.salad_worker_artifact_region
    if bucket_setting is None or region_setting is None:
        return None
    endpoint_setting = settings.salad_worker_artifact_endpoint_url
    if endpoint_setting is not None:
        endpoint = urlsplit(endpoint_setting.get_secret_value())
        source = f"{endpoint.scheme}://{endpoint.netloc}"
        return source if CSP_CONNECT_SOURCE_PATTERN.fullmatch(source) else None
    bucket = bucket_setting.get_secret_value()
    region = region_setting.get_secret_value()
    aws_suffix = "amazonaws.com.cn" if region.startswith("cn-") else "amazonaws.com"
    host = (
        f"{bucket}.s3.{aws_suffix}"
        if region == "us-east-1"
        else f"{bucket}.s3.{region}.{aws_suffix}"
    )
    source = f"https://{host}"
    return source if CSP_CONNECT_SOURCE_PATTERN.fullmatch(source) else None


def content_security_policy(
    environment: Environment,
    *,
    allow_same_origin_scripts: bool = False,
    asset_connect_source: str | None = None,
    model_artifact_connect_source: str | None = None,
) -> str:
    image_sources = "'self' https:"
    connect_sources = ["'self'"]
    if environment in {Environment.LOCAL, Environment.TEST}:
        image_sources = f"{image_sources} http:"
    for label, source in (
        ("asset", asset_connect_source),
        ("model artifact", model_artifact_connect_source),
    ):
        if source is None:
            continue
        if CSP_CONNECT_SOURCE_PATTERN.fullmatch(source) is None:
            raise ValueError(f"invalid CSP {label} connection source")
        if source not in connect_sources:
            connect_sources.append(source)
    script_sources = "'self'" if allow_same_origin_scripts else "'none'"
    return (
        f"default-src 'self'; base-uri 'none'; connect-src {' '.join(connect_sources)}; "
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
        if not _is_private_revalidated_preview_response(request, response):
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
            model_artifact_connect_source=(
                model_artifact_connection_source(settings)
                if settings.lora_manager_enabled
                and (
                    request.url.path == "/dashboard/loras"
                    or request.url.path.startswith("/dashboard/loras/")
                )
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


def _is_private_revalidated_preview_response(request: Request, response: Response) -> bool:
    """Preserve only the private, auth-revalidated JPEG cache contract."""

    cache_control = response.headers.get("cache-control", "").lower()
    vary = {
        value.strip().lower()
        for value in response.headers.get("vary", "").split(",")
        if value.strip()
    }
    content_type = response.headers.get("content-type", "").partition(";")[0].lower()
    return (
        request.method in {"GET", "HEAD"}
        and DASHBOARD_PREVIEW_PATH_PATTERN.fullmatch(request.url.path) is not None
        and response.status_code in {200, 304}
        and (response.status_code == 304 or content_type == "image/jpeg")
        and "private" in {part.strip() for part in cache_control.split(",")}
        and "no-cache" in {part.strip() for part in cache_control.split(",")}
        and "must-revalidate" in {part.strip() for part in cache_control.split(",")}
        and "cookie" in vary
        and response.headers.get("x-content-type-options", "").lower() == "nosniff"
    )
