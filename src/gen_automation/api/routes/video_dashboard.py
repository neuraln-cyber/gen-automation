from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated
from urllib.parse import parse_qs
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from gen_automation.api.security import (
    ReleaseReader,
    authentication_service,
    require_release_manager,
)
from gen_automation.config import Settings
from gen_automation.db.session import get_session
from gen_automation.domain.enums import AdminRole
from gen_automation.domain.video import VideoContentRating
from gen_automation.middleware import content_security_policy
from gen_automation.services.authentication import (
    AuthenticatedPrincipal,
    CsrfValidationError,
)
from gen_automation.services.dashboard_previews import (
    DASHBOARD_PREVIEW_CACHE_CONTROL,
    DASHBOARD_PREVIEW_CONTENT_TYPE,
    DashboardPreviewConflictError,
    DashboardPreviewNotFoundError,
    DashboardPreviewRenderError,
    DashboardPreviewStorageError,
    load_or_create_dashboard_preview,
)
from gen_automation.services.video_runtime import request_video_cancellation
from gen_automation.services.videos import (
    CreateVideoSubmission,
    VideoStudioConflictError,
    VideoStudioError,
    VideoStudioInputError,
    VideoStudioNotFoundError,
    VideoStudioStorageError,
    create_video_submission,
    format_microusd,
    list_video_sources,
    load_video_submission,
    planning_estimate_microusd,
    presign_video_output,
)
from gen_automation.storage.base import ObjectStore, ObjectStoreError

router = APIRouter(prefix="/dashboard/animations", tags=["dashboard"], include_in_schema=False)
templates = Jinja2Templates(directory=str(Path(__file__).parents[2] / "templates"))
Session = Annotated[AsyncSession, Depends(get_session)]
_MANAGER_ROLES = frozenset({AdminRole.OWNER, AdminRole.ADMIN})
_FORM_CONTENT_TYPE = "application/x-www-form-urlencoded"
_MAX_FORM_BODY_BYTES = 16 * 1024
_FORM_KEY = re.compile(r"video-studio-[0-9a-f]{64}")
_HEX = frozenset("0123456789abcdefABCDEF")
_REQUIRED_FORM_FIELDS = frozenset(
    {
        "csrf_token",
        "submission_id",
        "idempotency_key",
        "source_asset_id",
        "prompt",
        "content_rating",
        "duration_seconds",
        "variant_count",
    }
)
_ATTESTATION_FIELDS = frozenset(
    {
        "source_rights_confirmed",
        "lawful_use_confirmed",
        "all_depicted_people_are_adults",
        "consensual_adult_content_confirmed",
        "no_real_person_sexual_content",
    }
)


class BrowserVideoFormError(ValueError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int,
        values: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.values = values or {}


@dataclass(frozen=True, slots=True)
class BrowserVideoForm:
    csrf_token: str
    submission_id: UUID
    idempotency_key: str
    command: CreateVideoSubmission
    values: dict[str, str]


@router.get("", response_class=HTMLResponse, name="dashboard_video_new")
async def dashboard_video_new(
    request: Request,
    session: Session,
    principal: ReleaseReader,
) -> Response:
    unavailable = _availability_response(request, principal)
    if unavailable is not None:
        return unavailable
    try:
        sources = await list_video_sources(
            session,
            actor_user_id=principal.user_id,
        )
        csrf_token = _form_csrf_token(request, principal)
    except CsrfValidationError:
        return _error_response(
            request,
            principal=principal,
            status_code=status.HTTP_403_FORBIDDEN,
            heading="Animation Studio could not be opened",
            message="The browser session could not be verified. Sign in again and retry.",
        )
    except VideoStudioError:
        return _error_response(
            request,
            principal=principal,
            status_code=status.HTTP_403_FORBIDDEN,
            heading="Animation Studio is unavailable",
            message="Your operator account cannot create animations.",
        )
    return _video_form_response(
        request,
        principal=principal,
        sources=sources,
        csrf_token=csrf_token,
    )


@router.post(
    "",
    response_class=HTMLResponse,
    response_model=None,
    name="dashboard_video_submit",
)
async def submit_dashboard_video(
    request: Request,
    session: Session,
    principal: ReleaseReader,
) -> Response:
    unavailable = _availability_response(request, principal)
    if unavailable is not None:
        return unavailable
    try:
        form = await _read_video_form(request)
    except BrowserVideoFormError as error:
        return await _submission_error(
            request,
            session=session,
            principal=principal,
            values=error.values,
            message=error.message,
            status_code=error.status_code,
        )

    try:
        manager = await require_release_manager(
            request,
            session,
            csrf_header=form.csrf_token,
        )
        if manager.role not in _MANAGER_ROLES:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="animation manager role required",
            )
        settings: Settings = request.app.state.settings
        if not settings.auth_enabled and not hmac.compare_digest(
            form.csrf_token,
            _development_csrf_token(settings, session_id=manager.session_id),
        ):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="CSRF validation failed",
            )
        expected_key = _video_form_key(
            settings,
            session_id=manager.session_id,
            submission_id=form.submission_id,
        )
        if not hmac.compare_digest(form.idempotency_key, expected_key):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="form idempotency validation failed",
            )
        submission = await create_video_submission(
            session,
            command=form.command,
            actor_user_id=manager.user_id,
            max_hourly_cost_usd=settings.salad_video_max_hourly_cost_usd,
        )
        await session.commit()
    except HTTPException as error:
        await session.rollback()
        return await _submission_error(
            request,
            session=session,
            principal=principal,
            values=form.values,
            message="The browser session or form could not be verified. Reload and try again.",
            status_code=error.status_code,
        )
    except VideoStudioInputError as error:
        await session.rollback()
        return await _submission_error(
            request,
            session=session,
            principal=principal,
            values=form.values,
            message=str(error).capitalize() + ".",
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )
    except VideoStudioNotFoundError:
        await session.rollback()
        return await _submission_error(
            request,
            session=session,
            principal=principal,
            values=form.values,
            message="The selected source image is no longer available. Choose another image.",
            status_code=status.HTTP_404_NOT_FOUND,
        )
    except (VideoStudioConflictError, IntegrityError):
        await session.rollback()
        return await _submission_error(
            request,
            session=session,
            principal=principal,
            values=form.values,
            message=(
                "The source image or saved form changed while this animation was being queued. "
                "Reload and try again."
            ),
            status_code=status.HTTP_409_CONFLICT,
        )

    primary = submission.jobs[0]
    return _secure_response(
        request,
        RedirectResponse(
            url=f"/dashboard/animations/{primary.id}",
            status_code=status.HTTP_303_SEE_OTHER,
        ),
    )


@router.get(
    "/{job_id}",
    response_class=HTMLResponse,
    name="dashboard_video_status",
)
async def dashboard_video_status(
    job_id: UUID,
    request: Request,
    session: Session,
    principal: ReleaseReader,
) -> Response:
    unavailable = _availability_response(request, principal)
    if unavailable is not None:
        return unavailable
    try:
        submission = await load_video_submission(
            session,
            job_id=job_id,
            actor_user_id=principal.user_id,
        )
    except VideoStudioNotFoundError:
        return _error_response(
            request,
            principal=principal,
            status_code=status.HTTP_404_NOT_FOUND,
            heading="Animation not found",
            message="This animation does not exist or belongs to another operator.",
        )
    except VideoStudioConflictError:
        return _error_response(
            request,
            principal=principal,
            status_code=status.HTTP_409_CONFLICT,
            heading="Animation status is unavailable",
            message="The saved animation record is incomplete. Retry after the worker reconciles.",
        )
    try:
        csrf_token = _form_csrf_token(request, principal)
    except CsrfValidationError:
        return _error_response(
            request,
            principal=principal,
            status_code=status.HTTP_403_FORBIDDEN,
            heading="Animation status is unavailable",
            message="The browser session could not be verified. Sign in again and retry.",
        )
    return _secure_response(
        request,
        templates.TemplateResponse(
            request=request,
            name="dashboard/video_status.html",
            context={
                "page_title": "Animation status",
                "principal": principal,
                "submission": submission,
                "format_cost": format_microusd,
                "csrf_token": csrf_token,
            },
        ),
    )


@router.post(
    "/{job_id}/cancel",
    response_class=HTMLResponse,
    response_model=None,
    name="dashboard_video_cancel",
)
async def cancel_dashboard_video(
    job_id: UUID,
    request: Request,
    session: Session,
    principal: ReleaseReader,
) -> Response:
    unavailable = _availability_response(request, principal)
    if unavailable is not None:
        return unavailable
    try:
        values = await _read_exact_form(
            request,
            required_fields=frozenset({"csrf_token"}),
            allowed_fields=frozenset({"csrf_token"}),
        )
        csrf_token = _bounded(values["csrf_token"], maximum=200, label="Security token")
        manager = await require_release_manager(
            request,
            session,
            csrf_header=csrf_token,
        )
        if manager.role not in _MANAGER_ROLES:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="animation manager role required",
            )
        settings: Settings = request.app.state.settings
        if not settings.auth_enabled and not hmac.compare_digest(
            csrf_token,
            _development_csrf_token(settings, session_id=manager.session_id),
        ):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="CSRF validation failed",
            )
        submission = await load_video_submission(
            session,
            job_id=job_id,
            actor_user_id=manager.user_id,
        )
        for job in submission.jobs:
            cancelled = await request_video_cancellation(
                session,
                job_id=job.id,
                actor_user_id=manager.user_id,
            )
            if not cancelled:
                raise VideoStudioConflictError("animation cancellation target changed")
        await session.commit()
    except BrowserVideoFormError as error:
        await session.rollback()
        return _error_response(
            request,
            principal=principal,
            status_code=error.status_code,
            heading="Animation could not be cancelled",
            message=error.message,
        )
    except HTTPException as error:
        await session.rollback()
        return _error_response(
            request,
            principal=principal,
            status_code=error.status_code,
            heading="Animation could not be cancelled",
            message="The browser session could not be verified. Reload and try again.",
        )
    except VideoStudioNotFoundError:
        await session.rollback()
        return _error_response(
            request,
            principal=principal,
            status_code=status.HTTP_404_NOT_FOUND,
            heading="Animation not found",
            message="This animation does not exist or belongs to another operator.",
        )
    except VideoStudioConflictError:
        await session.rollback()
        return _error_response(
            request,
            principal=principal,
            status_code=status.HTTP_409_CONFLICT,
            heading="Animation could not be cancelled",
            message="The saved animation changed while cancellation was requested.",
        )
    return _secure_response(
        request,
        RedirectResponse(
            url=f"/dashboard/animations/{job_id}",
            status_code=status.HTTP_303_SEE_OTHER,
        ),
    )


@router.get(
    "/{job_id}/output",
    response_class=Response,
    response_model=None,
    name="dashboard_video_output",
)
async def dashboard_video_output(
    job_id: UUID,
    request: Request,
    session: Session,
    principal: ReleaseReader,
) -> Response:
    unavailable = _availability_response(request, principal)
    if unavailable is not None:
        return unavailable
    store: ObjectStore | None = request.app.state.object_store
    if store is None:
        return _json_error(
            request,
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="private video storage is unavailable",
        )
    settings: Settings = request.app.state.settings
    try:
        url = await presign_video_output(
            session,
            store,
            job_id=job_id,
            actor_user_id=principal.user_id,
            expires_in=min(settings.storage_presign_ttl_seconds, 900),
        )
    except VideoStudioNotFoundError:
        return _json_error(
            request,
            status_code=status.HTTP_404_NOT_FOUND,
            detail="animation output not found",
        )
    except VideoStudioConflictError:
        return _json_error(
            request,
            status_code=status.HTTP_409_CONFLICT,
            detail="animation output is not available",
        )
    except VideoStudioStorageError:
        return _json_error(
            request,
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="private video storage is unavailable",
        )
    except ObjectStoreError:
        return _json_error(
            request,
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="private video storage is unavailable",
        )
    return _secure_response(
        request,
        RedirectResponse(url=url, status_code=status.HTTP_307_TEMPORARY_REDIRECT),
    )


@router.get(
    "/assets/{asset_id}/preview/{source_token}.jpg",
    response_class=Response,
    response_model=None,
    name="dashboard_video_source_preview",
)
async def dashboard_video_source_preview(
    asset_id: UUID,
    source_token: str,
    request: Request,
    session: Session,
    principal: ReleaseReader,
) -> Response:
    unavailable = _availability_response(request, principal)
    if unavailable is not None:
        return unavailable
    store: ObjectStore | None = request.app.state.object_store
    if store is None:
        return _json_error(
            request,
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="private image storage is unavailable",
        )
    settings: Settings = request.app.state.settings
    try:
        preview = await load_or_create_dashboard_preview(
            session,
            store,
            asset_id=asset_id,
            source_token=source_token,
            max_master_bytes=settings.storage_max_image_bytes,
        )
    except DashboardPreviewNotFoundError:
        return _json_error(
            request,
            status_code=status.HTTP_404_NOT_FOUND,
            detail="animation source preview not found",
        )
    except DashboardPreviewConflictError:
        return _json_error(
            request,
            status_code=status.HTTP_409_CONFLICT,
            detail="animation source preview is unavailable",
        )
    except (DashboardPreviewStorageError, DashboardPreviewRenderError):
        return _json_error(
            request,
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="animation source preview is temporarily unavailable",
        )
    headers = {
        "Cache-Control": DASHBOARD_PREVIEW_CACHE_CONTROL,
        "Content-Disposition": "inline",
        "ETag": preview.etag,
        "Vary": "Cookie",
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": content_security_policy(settings.environment),
    }
    if _etag_matches(request.headers.get("if-none-match"), preview.etag):
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers=headers)
    return Response(
        content=preview.data,
        media_type=DASHBOARD_PREVIEW_CONTENT_TYPE,
        headers=headers,
    )


async def _read_video_form(request: Request) -> BrowserVideoForm:
    values = await _read_exact_form(request)
    try:
        csrf_token = _bounded(values["csrf_token"], maximum=200, label="Security token")
        submission_id = _uuid(values["submission_id"], label="Submission")
        idempotency_key = values["idempotency_key"]
        if _FORM_KEY.fullmatch(idempotency_key) is None:
            raise _bad_request("The form expired or was changed. Reload and try again.")
        source_asset_id = _uuid(values["source_asset_id"], label="Source image")
        prompt = values["prompt"].strip()
        if len(prompt) > 4_000:
            raise _unprocessable("Motion prompt must be at most 4,000 characters.")
        try:
            content_rating = VideoContentRating(values["content_rating"])
        except ValueError:
            raise _unprocessable("Content rating is invalid.") from None
        duration_seconds = _integer(values["duration_seconds"], label="Duration")
        variant_count = _integer(values["variant_count"], label="Variants")
        attestations = {field: _checkbox(values, field=field) for field in _ATTESTATION_FIELDS}
        command = CreateVideoSubmission(
            submission_id=submission_id,
            source_asset_id=source_asset_id,
            prompt=prompt,
            content_rating=content_rating,
            duration_seconds=duration_seconds,
            variant_count=variant_count,
            source_rights_confirmed=attestations["source_rights_confirmed"],
            lawful_use_confirmed=attestations["lawful_use_confirmed"],
            all_depicted_people_are_adults=attestations["all_depicted_people_are_adults"],
            consensual_adult_content_confirmed=attestations["consensual_adult_content_confirmed"],
            no_real_person_sexual_content=attestations["no_real_person_sexual_content"],
        )
    except BrowserVideoFormError as error:
        error.values = values
        raise
    return BrowserVideoForm(
        csrf_token=csrf_token,
        submission_id=submission_id,
        idempotency_key=idempotency_key,
        command=command,
        values=values,
    )


async def _read_exact_form(
    request: Request,
    *,
    required_fields: frozenset[str] = _REQUIRED_FORM_FIELDS,
    allowed_fields: frozenset[str] = _REQUIRED_FORM_FIELDS | _ATTESTATION_FIELDS,
) -> dict[str, str]:
    content_type = request.headers.get("content-type", "")
    if content_type.partition(";")[0].strip().lower() != _FORM_CONTENT_TYPE:
        raise BrowserVideoFormError(
            "The submitted form type is not supported.",
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
        )
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            declared_length = int(content_length)
        except ValueError:
            raise _bad_request("The submitted form length was invalid.") from None
        if declared_length < 0:
            raise _bad_request("The submitted form length was invalid.")
        if declared_length > _MAX_FORM_BODY_BYTES:
            raise BrowserVideoFormError(
                "The submitted form was too large.",
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            )
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > _MAX_FORM_BODY_BYTES:
            raise BrowserVideoFormError(
                "The submitted form was too large.",
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            )
        body.extend(chunk)
    try:
        encoded = bytes(body).decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise _bad_request("The submitted form was not valid UTF-8.") from None
    if not _valid_percent_encoding(encoded):
        raise _bad_request("The submitted form encoding was invalid.")
    try:
        parsed = parse_qs(
            encoded,
            keep_blank_values=True,
            strict_parsing=True,
            encoding="utf-8",
            errors="strict",
            max_num_fields=len(allowed_fields),
        )
    except (UnicodeDecodeError, ValueError):
        raise _bad_request("The submitted form fields were invalid.") from None
    submitted_fields = set(parsed)
    if (
        not required_fields.issubset(submitted_fields)
        or not submitted_fields.issubset(allowed_fields)
        or any(len(items) != 1 for items in parsed.values())
    ):
        raise _bad_request("The submitted form fields were invalid.")
    return {field: items[0] for field, items in parsed.items()}


async def _submission_error(
    request: Request,
    *,
    session: AsyncSession,
    principal: AuthenticatedPrincipal,
    values: dict[str, str],
    message: str,
    status_code: int,
) -> Response:
    # Attestations are intentionally not sticky.  A corrected submission must
    # be consciously confirmed again instead of inheriting a prior checkbox.
    redisplay_values = dict(values)
    for field in _ATTESTATION_FIELDS:
        redisplay_values.pop(field, None)
    try:
        sources = await list_video_sources(
            session,
            actor_user_id=principal.user_id,
        )
        csrf_token = _form_csrf_token(request, principal)
    except (CsrfValidationError, VideoStudioError):
        return _error_response(
            request,
            principal=principal,
            status_code=status.HTTP_403_FORBIDDEN,
            heading="Animation Studio is unavailable",
            message="The browser session or operator account could not be verified.",
        )
    return _video_form_response(
        request,
        principal=principal,
        sources=sources,
        csrf_token=csrf_token,
        values=redisplay_values,
        error_message=message,
        status_code=status_code,
    )


def _video_form_response(
    request: Request,
    *,
    principal: AuthenticatedPrincipal,
    sources: object,
    csrf_token: str,
    values: dict[str, str] | None = None,
    error_message: str | None = None,
    status_code: int = status.HTTP_200_OK,
) -> Response:
    settings: Settings = request.app.state.settings
    submitted = values or {}
    if submitted.get("submission_id"):
        try:
            submission_id = UUID(submitted["submission_id"])
        except ValueError:
            submission_id = uuid4()
    else:
        submission_id = uuid4()
    idempotency_key = _video_form_key(
        settings,
        session_id=principal.session_id,
        submission_id=submission_id,
    )
    estimate = planning_estimate_microusd(
        max_hourly_cost_usd=settings.salad_video_max_hourly_cost_usd,
        duration_seconds=3,
        variant_count=1,
    )
    return _secure_response(
        request,
        templates.TemplateResponse(
            request=request,
            name="dashboard/video_new.html",
            context={
                "page_title": "Animation Studio",
                "principal": principal,
                "sources": sources,
                "csrf_token": csrf_token,
                "submission_id": submission_id,
                "idempotency_key": idempotency_key,
                "values": submitted,
                "error_message": error_message,
                "hourly_rate_usd": str(settings.salad_video_max_hourly_cost_usd),
                "default_estimate": format_microusd(estimate),
            },
            status_code=status_code,
        ),
    )


def _availability_response(
    request: Request,
    principal: AuthenticatedPrincipal,
) -> Response | None:
    if principal.role not in _MANAGER_ROLES:
        return _error_response(
            request,
            principal=principal,
            status_code=status.HTTP_403_FORBIDDEN,
            heading="Animation Studio is unavailable",
            message="Your account cannot create animations.",
        )
    settings: Settings = request.app.state.settings
    if not settings.video_generation_enabled:
        return _error_response(
            request,
            principal=principal,
            status_code=status.HTTP_404_NOT_FOUND,
            heading="Animation Studio is not enabled",
            message="The private video worker is not enabled in this environment yet.",
        )
    return None


def _form_csrf_token(
    request: Request,
    principal: AuthenticatedPrincipal,
) -> str:
    settings: Settings = request.app.state.settings
    if not settings.auth_enabled:
        return _development_csrf_token(settings, session_id=principal.session_id)
    cookie_token = request.cookies.get(settings.auth_csrf_cookie_name)
    if cookie_token is None:
        raise CsrfValidationError("CSRF cookie is unavailable")
    authentication_service(request).validate_csrf(
        principal,
        cookie_token=cookie_token,
        header_token=cookie_token,
    )
    return cookie_token


def _video_form_key(
    settings: Settings,
    *,
    session_id: UUID,
    submission_id: UUID,
) -> str:
    return _signed_form_value(
        settings,
        session_id=session_id,
        action="submit",
        value=str(submission_id),
    )


def _development_csrf_token(settings: Settings, *, session_id: UUID) -> str:
    return _signed_form_value(
        settings,
        session_id=session_id,
        action="csrf",
        value="",
    )


def _signed_form_value(
    settings: Settings,
    *,
    session_id: UUID,
    action: str,
    value: str,
) -> str:
    context = "\x1f".join(("gen-automation-browser-video-v1", str(session_id), action, value))
    digest = hmac.new(
        settings.session_secret.get_secret_value().encode("utf-8"),
        context.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"video-studio-{digest}"


def _checkbox(values: dict[str, str], *, field: str) -> bool:
    value = values.get(field)
    if value is None:
        return False
    if value != "on":
        raise _bad_request("The submitted attestation fields were invalid.")
    return True


def _uuid(value: str, *, label: str) -> UUID:
    try:
        parsed = UUID(value)
    except ValueError:
        raise _unprocessable(f"{label} selection is invalid.") from None
    if str(parsed) != value.lower():
        raise _unprocessable(f"{label} selection is invalid.")
    return parsed


def _integer(value: str, *, label: str) -> int:
    if not value or not value.isascii():
        raise _unprocessable(f"{label} must be a whole number.")
    try:
        return int(value)
    except ValueError:
        raise _unprocessable(f"{label} must be a whole number.") from None


def _bounded(value: str, *, maximum: int, label: str) -> str:
    if not 1 <= len(value) <= maximum:
        raise _bad_request(f"{label} is invalid.")
    return value


def _valid_percent_encoding(value: str) -> bool:
    index = 0
    while index < len(value):
        if value[index] != "%":
            index += 1
            continue
        if index + 2 >= len(value) or value[index + 1] not in _HEX or value[index + 2] not in _HEX:
            return False
        index += 3
    return True


def _etag_matches(supplied: str | None, expected: str) -> bool:
    if supplied is None:
        return False
    return any(item.strip() in {expected, f"W/{expected}"} for item in supplied.split(","))


def _bad_request(message: str) -> BrowserVideoFormError:
    return BrowserVideoFormError(message, status_code=status.HTTP_400_BAD_REQUEST)


def _unprocessable(message: str) -> BrowserVideoFormError:
    return BrowserVideoFormError(
        message,
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
    )


def _error_response(
    request: Request,
    *,
    principal: AuthenticatedPrincipal,
    status_code: int,
    heading: str,
    message: str,
) -> Response:
    return _secure_response(
        request,
        templates.TemplateResponse(
            request=request,
            name="dashboard/error.html",
            context={
                "page_title": heading,
                "principal": principal,
                "heading": heading,
                "message": message,
            },
            status_code=status_code,
        ),
    )


def _json_error(request: Request, *, status_code: int, detail: str) -> Response:
    return _secure_response(
        request,
        JSONResponse(status_code=status_code, content={"detail": detail}),
    )


def _secure_response(request: Request, response: Response) -> Response:
    settings: Settings = request.app.state.settings
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = content_security_policy(settings.environment)
    return response
