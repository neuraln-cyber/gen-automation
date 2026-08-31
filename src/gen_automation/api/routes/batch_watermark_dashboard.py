"""Owner-only, transient batch watermarking workspace."""

from __future__ import annotations

import hmac
import json
import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path, PurePath
from tempfile import SpooledTemporaryFile
from typing import Annotated, cast
from uuid import UUID
from zipfile import ZIP_STORED, ZipFile

from anyio import fail_after, to_process, to_thread
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.datastructures import FormData, UploadFile
from starlette.formparsers import MultiPartException

from gen_automation.api.browser_delivery_forms import delivery_csrf_token
from gen_automation.api.security import (
    PublicationReader,
    authentication_service,
    require_publication_mutation_owner,
)
from gen_automation.config import Settings
from gen_automation.db.session import get_session
from gen_automation.domain.enums import AdminRole
from gen_automation.middleware import content_security_policy
from gen_automation.services.authentication import AuthenticatedPrincipal, CsrfValidationError
from gen_automation.services.derivatives import (
    DerivativeError,
    FullResolutionWatermarkedImage,
    WatermarkPosition,
    render_full_resolution_watermark,
)
from gen_automation.services.watermarks import (
    RegisteredWatermark,
    WatermarkConflictError,
    WatermarkInputError,
    WatermarkNotFoundError,
    WatermarkStorageError,
    list_registered_watermarks,
    read_registered_watermark,
)
from gen_automation.storage.base import ObjectStore, ObjectStoreError
from gen_automation.storage.images import ImageVerificationError, VerifiedImage, verify_image_bytes

router = APIRouter(prefix="/dashboard", tags=["dashboard"], include_in_schema=False)
templates = Jinja2Templates(directory=str(Path(__file__).parents[2] / "templates"))
Session = Annotated[AsyncSession, Depends(get_session)]

MAX_BATCH_FILES = 300
MAX_IMAGE_BYTES = 32 * 1024 * 1024
MAX_BATCH_BODY_BYTES = 2 * 1024 * 1024 * 1024
_ARCHIVE_SPOOL_BYTES = 64 * 1024 * 1024
_STREAM_CHUNK_BYTES = 1024 * 1024
_SAFE_STEM = re.compile(r"[^A-Za-z0-9._-]+")
_ARCHIVE_KINDS = frozenset({"both", "watermarked", "originals"})
_FORM_FIELDS = frozenset(
    {"csrf_token", "watermark_asset_id", "watermark_placements", "archive_kind", "images"}
)


@router.get(
    "/watermarking",
    response_class=HTMLResponse,
    name="dashboard_batch_watermarking",
)
async def dashboard_batch_watermarking(
    request: Request,
    session: Session,
    principal: PublicationReader,
) -> Response:
    if principal.role != AdminRole.OWNER:
        return _error(
            request,
            principal,
            status_code=status.HTTP_403_FORBIDDEN,
            heading="Watermarking is owner-only",
            message="Only the owner can use the transient batch watermarking workspace.",
        )
    store = _store(request)
    registered: tuple[RegisteredWatermark, ...] = ()
    storage_error: str | None = None
    if store is None:
        storage_error = "Private storage is unavailable, so saved watermarks cannot be loaded."
    else:
        try:
            registered = await list_registered_watermarks(session)
        except WatermarkInputError:
            storage_error = "Saved watermarks could not be listed."
    try:
        csrf_token = _csrf_token(request, principal)
    except CsrfValidationError:
        return _error(
            request,
            principal,
            status_code=status.HTTP_403_FORBIDDEN,
            heading="Session validation failed",
            message="Reload the page and try again.",
        )
    watermarks = tuple(
        {
            "asset_id": item.asset_id,
            "display_name": item.display_name,
            "width": item.width,
            "height": item.height,
            "preview_url": f"/dashboard/watermarking/watermarks/{item.asset_id}/preview",
        }
        for item in registered
    )
    return _secure(
        request,
        templates.TemplateResponse(
            request=request,
            name="dashboard/watermarking.html",
            context={
                "page_title": "Batch watermarking",
                "principal": principal,
                "csrf_token": csrf_token,
                "watermarks": watermarks,
                "storage_error": storage_error,
                "max_batch_files": MAX_BATCH_FILES,
            },
        ),
    )


@router.get(
    "/watermarking/watermarks/{asset_id}/preview",
    response_class=Response,
    response_model=None,
    name="dashboard_batch_watermark_preview",
)
async def dashboard_batch_watermark_preview(
    asset_id: UUID,
    request: Request,
    session: Session,
    principal: PublicationReader,
) -> Response:
    if principal.role != AdminRole.OWNER:
        return _media_error(request, status.HTTP_403_FORBIDDEN, "Access denied.")
    store = _store(request)
    if store is None:
        return _media_error(
            request,
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Private storage is unavailable.",
        )
    try:
        payload = await read_registered_watermark(session, store, asset_id=asset_id)
    except WatermarkNotFoundError:
        return _media_error(request, status.HTTP_404_NOT_FOUND, "Preview not found.")
    except WatermarkConflictError:
        return _media_error(
            request,
            status.HTTP_409_CONFLICT,
            "The saved watermark is not currently available.",
        )
    except (WatermarkStorageError, ObjectStoreError):
        return _media_error(
            request,
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Private storage is temporarily unavailable.",
        )
    response = Response(
        content=payload.data,
        media_type="image/png",
        headers={
            "Content-Disposition": f'inline; filename="watermark-{payload.asset_id}.png"',
            "X-Content-Type-Options": "nosniff",
            "Cross-Origin-Resource-Policy": "same-origin",
            "Referrer-Policy": "no-referrer",
            "Vary": "Cookie",
        },
    )
    return _secure(request, response)


@router.post(
    "/watermarking:download",
    response_class=StreamingResponse,
    response_model=None,
    name="dashboard_batch_watermark_download",
)
async def dashboard_batch_watermark_download(
    request: Request,
    session: Session,
    principal: PublicationReader,
) -> Response:
    form: FormData | None = None
    archive: SpooledTemporaryFile[bytes] | None = None
    try:
        (
            form,
            uploads,
            csrf_token,
            watermark_asset_id,
            placements,
            archive_kind,
        ) = await _read_batch_form(request)
        await _verified_mutation_owner(request, session, principal, csrf_token)
        watermark_png: bytes | None = None
        if archive_kind != "originals":
            if watermark_asset_id is None:
                raise _BatchFormError(status.HTTP_422_UNPROCESSABLE_CONTENT, "Choose a watermark.")
            store = _store(request)
            if store is None:
                raise _BatchFormError(
                    status.HTTP_503_SERVICE_UNAVAILABLE,
                    "Private storage is unavailable.",
                )
            try:
                watermark = await read_registered_watermark(
                    session,
                    store,
                    asset_id=watermark_asset_id,
                )
            except WatermarkNotFoundError:
                raise _BatchFormError(
                    status.HTTP_404_NOT_FOUND,
                    "The selected watermark was not found.",
                ) from None
            except WatermarkConflictError:
                raise _BatchFormError(
                    status.HTTP_409_CONFLICT,
                    "The selected watermark is not currently available.",
                ) from None
            except (WatermarkStorageError, ObjectStoreError):
                raise _BatchFormError(
                    status.HTTP_503_SERVICE_UNAVAILABLE,
                    "Private storage is temporarily unavailable.",
                ) from None
            watermark_png = watermark.data

        archive = SpooledTemporaryFile(max_size=_ARCHIVE_SPOOL_BYTES, mode="w+b")
        total_image_bytes = 0
        with ZipFile(archive, mode="w", compression=ZIP_STORED, allowZip64=True) as bundle:
            for index, upload in enumerate(uploads, start=1):
                source = await upload.read(MAX_IMAGE_BYTES + 1)
                if not source:
                    raise _BatchFormError(
                        status.HTTP_422_UNPROCESSABLE_CONTENT,
                        f"Image {index} is empty.",
                    )
                if len(source) > MAX_IMAGE_BYTES:
                    raise _BatchFormError(
                        status.HTTP_413_CONTENT_TOO_LARGE,
                        f"Image {index} exceeds 32 MiB.",
                    )
                total_image_bytes += len(source)
                if total_image_bytes > MAX_BATCH_BODY_BYTES:
                    raise _BatchFormError(
                        status.HTTP_413_CONTENT_TOO_LARGE,
                        "The selected batch is too large.",
                    )

                if archive_kind == "originals":
                    verified = await _verify_image(source)
                    original_extension = verified.extension
                else:
                    assert watermark_png is not None
                    rendered = await _render_watermarked(
                        source,
                        watermark_png,
                        placements[index - 1],
                    )
                    original_extension = _source_extension(source, rendered)
                    watermarked_name = _archive_name(
                        index,
                        upload.filename,
                        rendered.extension,
                        watermarked=True,
                    )
                    bundle.writestr(f"watermarked/{watermarked_name}", rendered.data)

                if archive_kind != "watermarked":
                    original_name = _archive_name(
                        index,
                        upload.filename,
                        original_extension,
                        watermarked=False,
                    )
                    bundle.writestr(f"originals/{original_name}", source)

        archive.seek(0)
        timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        response = StreamingResponse(
            _stream_archive(archive),
            media_type="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="watermark-batch-{timestamp}.zip"',
                "X-Content-Type-Options": "nosniff",
                "Cross-Origin-Resource-Policy": "same-origin",
                "Referrer-Policy": "no-referrer",
                "Vary": "Cookie",
            },
        )
        archive = None
        return _secure(request, response)
    except _BatchFormError as error:
        return _json_error(request, error.status_code, error.message)
    except HTTPException as error:
        message = "Authentication is required." if error.status_code == 401 else "Request denied."
        return _json_error(request, error.status_code, message)
    except (DerivativeError, ImageVerificationError):
        return _json_error(
            request,
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "One of the selected files is not a safe JPEG, PNG, or WebP image.",
        )
    except TimeoutError:
        return _json_error(
            request,
            status.HTTP_504_GATEWAY_TIMEOUT,
            "An image took too long to watermark. Try a smaller batch.",
        )
    finally:
        if archive is not None:
            archive.close()
        if form is not None:
            await form.close()


class _BatchFormError(Exception):
    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


async def _read_batch_form(
    request: Request,
) -> tuple[FormData, tuple[UploadFile, ...], str, UUID | None, tuple[str, ...], str]:
    content_type = request.headers.get("content-type", "")
    if content_type.partition(";")[0].strip().lower() != "multipart/form-data":
        raise _BatchFormError(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "Upload a multipart batch.")
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            declared = int(content_length)
        except ValueError:
            raise _BatchFormError(status.HTTP_400_BAD_REQUEST, "Invalid upload length.") from None
        if declared < 0:
            raise _BatchFormError(status.HTTP_400_BAD_REQUEST, "Invalid upload length.")
        if declared > MAX_BATCH_BODY_BYTES:
            raise _BatchFormError(
                status.HTTP_413_CONTENT_TOO_LARGE,
                "The selected batch is too large.",
            )
    try:
        form = await request.form(
            max_files=MAX_BATCH_FILES,
            max_fields=4,
            max_part_size=MAX_IMAGE_BYTES + 1,
        )
    except (HTTPException, MultiPartException, ValueError):
        raise _BatchFormError(status.HTTP_400_BAD_REQUEST, "The upload form is invalid.") from None

    try:
        items = list(form.multi_items())
        keys = {key for key, _ in items}
        field_items = tuple((key, value) for key, value in items if key != "images")
        uploads = tuple(value for key, value in items if key == "images")
        if (
            keys != _FORM_FIELDS
            or len(field_items) != 4
            or len({key for key, _ in field_items}) != 4
            or not uploads
            or len(uploads) > MAX_BATCH_FILES
            or any(not isinstance(item, UploadFile) for item in uploads)
        ):
            raise _BatchFormError(status.HTTP_400_BAD_REQUEST, "The upload form is invalid.")
        typed_uploads = cast(tuple[UploadFile, ...], uploads)
        csrf_token = _form_text(form.get("csrf_token"), 200, "CSRF token")
        archive_kind = _form_text(form.get("archive_kind"), 20, "archive kind")
        if archive_kind not in _ARCHIVE_KINDS:
            raise _BatchFormError(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "Choose a download type.",
            )
        watermark_value = _form_text(
            form.get("watermark_asset_id"),
            40,
            "watermark",
            allow_empty=True,
        )
        try:
            watermark_asset_id = UUID(watermark_value) if watermark_value else None
        except ValueError:
            raise _BatchFormError(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "Choose a valid watermark.",
            ) from None
        placements = _parse_placements(form.get("watermark_placements"), len(typed_uploads))
        return form, typed_uploads, csrf_token, watermark_asset_id, placements, archive_kind
    except BaseException:
        await form.close()
        raise


def _parse_placements(value: object, expected_count: int) -> tuple[str, ...]:
    text = _form_text(value, MAX_BATCH_FILES * 64, "watermark placements")
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        raise _BatchFormError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "Watermark placements are invalid.",
        ) from None
    if not isinstance(payload, list) or len(payload) != expected_count:
        raise _BatchFormError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "Choose a watermark corner for every image.",
        )
    positions: list[str] = []
    for expected_index, item in enumerate(payload):
        if (
            not isinstance(item, dict)
            or set(item) != {"index", "position"}
            or item.get("index") != expected_index
        ):
            raise _BatchFormError(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "Watermark placements are invalid.",
            )
        raw_position = item.get("position")
        if not isinstance(raw_position, str):
            raise _BatchFormError(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "Watermark placements are invalid.",
            )
        try:
            positions.append(WatermarkPosition(raw_position).value)
        except (TypeError, ValueError):
            raise _BatchFormError(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "Watermark placements are invalid.",
            ) from None
    return tuple(positions)


def _form_text(value: object, maximum: int, label: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise _BatchFormError(status.HTTP_400_BAD_REQUEST, f"The {label} is invalid.")
    normalized = value.strip()
    if (not normalized and not allow_empty) or len(normalized) > maximum:
        raise _BatchFormError(status.HTTP_400_BAD_REQUEST, f"The {label} is invalid.")
    return normalized


async def _render_watermarked(
    source: bytes,
    watermark: bytes,
    position: str,
) -> FullResolutionWatermarkedImage:
    with fail_after(120):
        rendered = await to_process.run_sync(
            render_full_resolution_watermark,
            source,
            watermark,
            position,
            cancellable=True,
        )
    if not isinstance(rendered, FullResolutionWatermarkedImage):
        raise DerivativeError("watermark rendering failed")
    return rendered


async def _verify_image(source: bytes) -> VerifiedImage:
    with fail_after(60):
        verified = await to_process.run_sync(verify_image_bytes, source, cancellable=True)
    if not isinstance(verified, VerifiedImage):
        raise ImageVerificationError("image verification failed")
    return verified


def _source_extension(source: bytes, rendered: FullResolutionWatermarkedImage) -> str:
    if source.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if source.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if source.startswith(b"RIFF") and source[8:12] == b"WEBP":
        return "webp"
    return rendered.extension


def _archive_name(
    index: int,
    original_name: str | None,
    extension: str,
    *,
    watermarked: bool,
) -> str:
    basename = PurePath((original_name or "image").replace("\\", "/")).name
    stem = basename.rsplit(".", maxsplit=1)[0] if "." in basename else basename
    stem = _SAFE_STEM.sub("-", stem).strip(" ._-")[:100] or "image"
    suffix = "-watermarked" if watermarked else ""
    return f"{index:03d}-{stem}{suffix}.{extension}"


async def _stream_archive(archive: SpooledTemporaryFile[bytes]) -> AsyncIterator[bytes]:
    try:
        while chunk := await to_thread.run_sync(archive.read, _STREAM_CHUNK_BYTES):
            yield chunk
    finally:
        archive.close()


async def _verified_mutation_owner(
    request: Request,
    session: AsyncSession,
    principal: AuthenticatedPrincipal,
    csrf_token: str,
) -> AuthenticatedPrincipal:
    if getattr(principal, "role", None) != AdminRole.OWNER:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
    owner = await require_publication_mutation_owner(
        request,
        session,
        csrf_header=csrf_token,
    )
    settings: Settings = request.app.state.settings
    if not settings.auth_enabled and not hmac.compare_digest(
        csrf_token,
        delivery_csrf_token(settings, session_id=owner.session_id),
    ):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
    return owner


def _csrf_token(request: Request, principal: AuthenticatedPrincipal) -> str:
    settings: Settings = request.app.state.settings
    session_id = principal.session_id
    if not settings.auth_enabled:
        return delivery_csrf_token(settings, session_id=session_id)
    token = request.cookies.get(settings.auth_csrf_cookie_name)
    authentication_service(request).validate_csrf(
        principal,
        cookie_token=token,
        header_token=token,
    )
    if token is None:
        raise CsrfValidationError
    return token


def _store(request: Request) -> ObjectStore | None:
    return cast(ObjectStore | None, getattr(request.app.state, "object_store", None))


def _json_error(request: Request, status_code: int, message: str) -> Response:
    return _secure(
        request,
        JSONResponse({"detail": message}, status_code=status_code),
    )


def _media_error(request: Request, status_code: int, message: str) -> Response:
    response = JSONResponse({"detail": message}, status_code=status_code)
    response.headers["X-Content-Type-Options"] = "nosniff"
    return _secure(request, response)


def _error(
    request: Request,
    principal: AuthenticatedPrincipal,
    *,
    status_code: int,
    heading: str,
    message: str,
) -> Response:
    return _secure(
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


def _secure(request: Request, response: Response) -> Response:
    response.headers["Cache-Control"] = "private, no-store"
    settings: Settings = request.app.state.settings
    response.headers["Content-Security-Policy"] = content_security_policy(settings.environment)
    return response
