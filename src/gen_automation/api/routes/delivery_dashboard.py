"""Owner dashboard for completed-set outputs and publication destinations."""

from __future__ import annotations

import hmac
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from typing import Annotated, cast
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException

from gen_automation.api.browser_delivery_forms import (
    BrowserDeliveryFormError,
    delivery_csrf_token,
    delivery_form_key,
    form_key_matches,
    read_finished_set_archive_download_form,
    read_package_download_form,
    read_patreon_confirm_absent_form,
    read_patreon_confirm_present_form,
    read_prepare_archive_form,
    read_prepare_destination_form,
    read_prepare_output_form,
    read_publication_guard_form,
)
from gen_automation.api.security import (
    PublicationReader,
    authentication_service,
    require_publication_mutation_owner,
    require_publication_owner,
)
from gen_automation.config import Settings
from gen_automation.db.session import get_session
from gen_automation.domain.enums import AdminRole, FinishedSetArchiveState
from gen_automation.middleware import content_security_policy
from gen_automation.services.authentication import (
    AuthenticatedPrincipal,
    CsrfValidationError,
)
from gen_automation.services.derivative_pipeline import (
    DerivativePipelineConflictError,
    DerivativePipelineInputError,
    DerivativePipelineNotFoundError,
)
from gen_automation.services.finished_set_archives import (
    FinishedSetArchiveConflictError,
    FinishedSetArchiveInputError,
    FinishedSetArchiveNotFoundError,
    FinishedSetArchiveSnapshot,
    load_finished_set_archive,
    presign_finished_set_archive_part,
    request_finished_set_archive,
)
from gen_automation.services.operator_delivery import (
    DeliveryOutput,
    OperatorDeliveryConflictError,
    OperatorDeliveryInputError,
    OperatorDeliveryNotFoundError,
    OperatorDeliverySnapshot,
    load_operator_delivery,
    package_parts_ready,
    prepare_operator_destinations,
)
from gen_automation.services.publication import (
    PUBLICATION_CONFIRM_PATREON_ABSENT_ATTESTATION,
    PUBLICATION_CONFIRM_PRESENT_ATTESTATION,
    PublicationConflictError,
    PublicationInputError,
    PublicationNotFoundError,
    presign_patreon_package_download,
    reconcile_publication_absent,
    reconcile_publication_present,
    set_publication_guard,
)
from gen_automation.services.review_derivatives import (
    prepare_completed_review_derivatives,
)
from gen_automation.services.watermarks import (
    MAX_WATERMARK_BYTES,
    WatermarkConflictError,
    WatermarkInputError,
    WatermarkNotFoundError,
    WatermarkStorageError,
    list_registered_watermarks,
    register_watermark,
)
from gen_automation.storage.base import ObjectStore, ObjectStoreError

router = APIRouter(prefix="/dashboard", tags=["dashboard"], include_in_schema=False)
templates = Jinja2Templates(directory=str(Path(__file__).parents[2] / "templates"))
Session = Annotated[AsyncSession, Depends(get_session)]
_MAX_MULTIPART_BYTES = MAX_WATERMARK_BYTES + (128 * 1024)


@dataclass(frozen=True, slots=True)
class PreviewView:
    output_id: UUID
    display_order: int
    width: int
    height: int
    url: str


@router.get(
    "/review-tasks/{review_task_id}/delivery",
    response_class=HTMLResponse,
    name="dashboard_review_delivery",
)
async def dashboard_review_delivery(
    review_task_id: UUID,
    request: Request,
    session: Session,
    principal: PublicationReader,
) -> Response:
    if principal.role != AdminRole.OWNER:
        return _error(
            request,
            principal,
            status_code=status.HTTP_403_FORBIDDEN,
            heading="Delivery is unavailable",
            message="Only the owner can prepare a completed set.",
        )
    try:
        snapshot = await load_operator_delivery(session, review_task_id=review_task_id)
        finished_set_archive = await load_finished_set_archive(
            session,
            review_task_id=review_task_id,
        )
        csrf_token = _csrf_token(request, principal)
        watermarks = await list_registered_watermarks(session)
    except (OperatorDeliveryNotFoundError, FinishedSetArchiveNotFoundError):
        return _error(
            request,
            principal,
            status_code=status.HTTP_404_NOT_FOUND,
            heading="Delivery not found",
            message="The completed review could not be found.",
        )
    except (CsrfValidationError, HTTPException):
        return _error(
            request,
            principal,
            status_code=status.HTTP_403_FORBIDDEN,
            heading="Session verification failed",
            message="Sign in again before preparing delivery.",
        )

    settings: Settings = request.app.state.settings
    output_submission_id = uuid4()
    archive_submission_id = uuid4()
    destination_submission_id = uuid4()
    watermark_submission_id = uuid4()
    guard_submission_id = uuid4()
    output_key = delivery_form_key(
        settings,
        session_id=principal.session_id,
        action="prepare-outputs",
        parts=(str(review_task_id), str(output_submission_id)),
    )
    archive_key = delivery_form_key(
        settings,
        session_id=principal.session_id,
        action="prepare-archive",
        parts=(str(review_task_id), str(archive_submission_id)),
    )
    destination_key = delivery_form_key(
        settings,
        session_id=principal.session_id,
        action="prepare-destinations",
        parts=(str(review_task_id), str(destination_submission_id)),
    )
    watermark_key = delivery_form_key(
        settings,
        session_id=principal.session_id,
        action="upload-watermark",
        parts=(str(review_task_id), str(watermark_submission_id)),
    )
    guard_target_enabled = not snapshot.publishing_guard_enabled
    guard_key = None
    if (
        snapshot.publishing_guard_epoch is not None
        and snapshot.publishing_guard_lock_version is not None
    ):
        guard_key = delivery_form_key(
            settings,
            session_id=principal.session_id,
            action="publication-guard",
            parts=(
                str(review_task_id),
                str(guard_submission_id),
                str(snapshot.publishing_guard_epoch),
                str(snapshot.publishing_guard_lock_version),
                "enabled" if guard_target_enabled else "stopped",
            ),
        )
    patreon_confirmation = next(
        (
            destination
            for destination in snapshot.destinations
            if destination.key == "patreon"
            and destination.state in {"ready", "unknown"}
            and destination.intent_id is not None
            and destination.intent_digest is not None
            and destination.intent_lock_version is not None
        ),
        None,
    )
    patreon_download_destination = next(
        (
            destination
            for destination in snapshot.destinations
            if destination.key == "patreon"
            and destination.state == "ready"
            and destination.intent_id is not None
            and destination.intent_digest is not None
            and destination.intent_lock_version is not None
            and package_parts_ready(destination.package_parts)
        ),
        None,
    )
    patreon_present_key = None
    patreon_absent_key = None
    if patreon_confirmation is not None:
        assert patreon_confirmation.intent_id is not None
        assert patreon_confirmation.intent_digest is not None
        assert patreon_confirmation.intent_lock_version is not None
        recovery_parts = (
            str(review_task_id),
            str(patreon_confirmation.intent_id),
            patreon_confirmation.intent_digest,
            str(patreon_confirmation.intent_lock_version),
        )
        patreon_present_key = delivery_form_key(
            settings,
            session_id=principal.session_id,
            action="patreon-confirm-present",
            parts=recovery_parts,
        )
        if patreon_confirmation.state == "unknown":
            patreon_absent_key = delivery_form_key(
                settings,
                session_id=principal.session_id,
                action="patreon-confirm-absent",
                parts=recovery_parts,
            )
    previews = (
        await _preview_views(request, snapshot.full_outputs)
        if _can_render_destination_form(snapshot, settings=settings)
        else ()
    )
    delivery_progress = _delivery_progress_payload(
        snapshot,
        finished_set_archive=finished_set_archive,
        mega_enabled=settings.mega_delivery_enabled,
    )
    return _secure(
        request,
        templates.TemplateResponse(
            request=request,
            name="dashboard/delivery.html",
            context={
                "page_title": f"Deliver {snapshot.release_title}",
                "principal": principal,
                "delivery": snapshot,
                "delivery_progress": delivery_progress,
                "watermarks": watermarks,
                "previews": previews,
                "csrf_token": csrf_token,
                "output_submission_id": output_submission_id,
                "archive_submission_id": archive_submission_id,
                "destination_submission_id": destination_submission_id,
                "watermark_submission_id": watermark_submission_id,
                "guard_submission_id": guard_submission_id,
                "output_idempotency_key": output_key,
                "archive_idempotency_key": archive_key,
                "destination_idempotency_key": destination_key,
                "public_preview_attested_at": datetime.now(UTC).isoformat(),
                "watermark_idempotency_key": watermark_key,
                "guard_idempotency_key": guard_key,
                "guard_target_enabled": guard_target_enabled,
                "patreon_present_idempotency_key": patreon_present_key,
                "patreon_absent_idempotency_key": patreon_absent_key,
                "patreon_present_attestation": PUBLICATION_CONFIRM_PRESENT_ATTESTATION,
                "patreon_absent_attestation": (PUBLICATION_CONFIRM_PATREON_ABSENT_ATTESTATION),
                "patreon_download_destination": patreon_download_destination,
                "finished_set_archive": (
                    finished_set_archive
                    if _finished_set_archive_parts_ready(finished_set_archive)
                    else None
                ),
                "finished_set_archive_snapshot": finished_set_archive,
                "storage_available": _store(request) is not None,
                "publishing_enabled": settings.publishing_enabled,
                "x_configured": settings.x_oauth_secret_reference is not None,
                "mega_configured": settings.mega_delivery_enabled,
            },
        ),
    )


@router.get(
    "/review-tasks/{review_task_id}/delivery/progress",
    response_class=JSONResponse,
    name="dashboard_review_delivery_progress",
)
async def dashboard_review_delivery_progress(
    review_task_id: UUID,
    request: Request,
    session: Session,
    principal: PublicationReader,
) -> Response:
    """Return lightweight output, archive, and MEGA progress for one completed review."""

    if principal.role != AdminRole.OWNER:
        return _secure(
            request,
            JSONResponse(
                status_code=status.HTTP_403_FORBIDDEN,
                content={"detail": "only the owner can view delivery progress"},
            ),
        )
    try:
        snapshot = await load_operator_delivery(session, review_task_id=review_task_id)
        finished_set_archive = await load_finished_set_archive(
            session,
            review_task_id=review_task_id,
        )
    except (OperatorDeliveryNotFoundError, FinishedSetArchiveNotFoundError):
        return _secure(
            request,
            JSONResponse(
                status_code=status.HTTP_404_NOT_FOUND,
                content={"detail": "completed review was not found"},
            ),
        )
    return _secure(
        request,
        JSONResponse(
            content=_delivery_progress_payload(
                snapshot,
                finished_set_archive=finished_set_archive,
                mega_enabled=request.app.state.settings.mega_delivery_enabled,
            )
        ),
    )


@router.post(
    "/review-tasks/{review_task_id}/delivery:publication-guard",
    response_class=HTMLResponse,
    response_model=None,
)
async def dashboard_change_publication_guard(
    review_task_id: UUID,
    request: Request,
    session: Session,
    principal: PublicationReader,
) -> Response:
    try:
        form = await read_publication_guard_form(request)
        owner = await (
            _verified_owner(request, session, principal, form.csrf_token)
            if form.enabled
            else _verified_mutation_owner(request, session, principal, form.csrf_token)
        )
        await load_operator_delivery(session, review_task_id=review_task_id)
        expected_key = delivery_form_key(
            request.app.state.settings,
            session_id=owner.session_id,
            action="publication-guard",
            parts=(
                str(review_task_id),
                str(form.submission_id),
                str(form.expected_epoch),
                str(form.expected_lock_version),
                "enabled" if form.enabled else "stopped",
            ),
        )
        if not form_key_matches(form.idempotency_key, expected_key):
            raise BrowserDeliveryFormError(status_code=status.HTTP_400_BAD_REQUEST)
        if form.enabled and not request.app.state.settings.publishing_enabled:
            raise BrowserDeliveryFormError(
                status_code=status.HTTP_409_CONFLICT,
                message=(
                    "Publication workers are disabled. Enable the publishing runtime "
                    "before opening the global publication switch."
                ),
            )
        await set_publication_guard(
            session,
            enabled=form.enabled,
            expected_epoch=form.expected_epoch,
            expected_lock_version=form.expected_lock_version,
            reason=form.reason,
            actor_user_id=owner.user_id,
            actor_role=owner.role,
            idempotency_key=form.idempotency_key,
        )
    except BrowserDeliveryFormError as error:
        return _form_error(request, principal, error.status_code, error.message)
    except HTTPException as error:
        if error.status_code == status.HTTP_401_UNAUTHORIZED:
            return _form_error(
                request,
                principal,
                status.HTTP_401_UNAUTHORIZED,
                "Confirm your password and authentication code before enabling publication.",
            )
        return _form_error(request, principal, status.HTTP_403_FORBIDDEN, "Request denied.")
    except OperatorDeliveryNotFoundError:
        return _form_error(
            request,
            principal,
            status.HTTP_404_NOT_FOUND,
            "The completed review could not be found.",
        )
    except PublicationInputError as error:
        return _form_error(
            request,
            principal,
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            str(error),
        )
    except PublicationConflictError:
        return _form_error(
            request,
            principal,
            status.HTTP_409_CONFLICT,
            "The publication switch changed in another session. Reload and try again.",
        )
    return _redirect(review_task_id)


@router.post(
    "/review-tasks/{review_task_id}/delivery:prepare-outputs",
    response_class=HTMLResponse,
    response_model=None,
)
async def dashboard_prepare_outputs(
    review_task_id: UUID,
    request: Request,
    session: Session,
    principal: PublicationReader,
) -> Response:
    try:
        form = await read_prepare_output_form(request)
        owner = await _verified_mutation_owner(request, session, principal, form.csrf_token)
        expected_key = delivery_form_key(
            request.app.state.settings,
            session_id=owner.session_id,
            action="prepare-outputs",
            parts=(str(review_task_id), str(form.submission_id)),
        )
        if not form_key_matches(form.idempotency_key, expected_key):
            raise BrowserDeliveryFormError(status_code=status.HTTP_400_BAD_REQUEST)
        await prepare_completed_review_derivatives(
            session,
            review_task_id=review_task_id,
            actor_user_id=owner.user_id,
            idempotency_key=form.idempotency_key,
            watermark_asset_id=form.watermark_asset_id,
        )
    except BrowserDeliveryFormError as error:
        return _form_error(request, principal, error.status_code, error.message)
    except HTTPException:
        return _form_error(request, principal, status.HTTP_403_FORBIDDEN, "Request denied.")
    except (
        DerivativePipelineInputError,
        DerivativePipelineNotFoundError,
        DerivativePipelineConflictError,
    ):
        return _form_error(
            request,
            principal,
            status.HTTP_409_CONFLICT,
            "Outputs could not be prepared from the current review snapshot.",
        )
    return _redirect(review_task_id)


@router.post(
    "/review-tasks/{review_task_id}/delivery:prepare-archive",
    response_class=HTMLResponse,
    response_model=None,
)
async def dashboard_prepare_finished_set_archive(
    review_task_id: UUID,
    request: Request,
    session: Session,
    principal: PublicationReader,
) -> Response:
    """Idempotently request the owner's destination-neutral finished-set ZIP."""

    try:
        form = await read_prepare_archive_form(request)
        owner = await _verified_mutation_owner(request, session, principal, form.csrf_token)
        expected_key = delivery_form_key(
            request.app.state.settings,
            session_id=owner.session_id,
            action="prepare-archive",
            parts=(str(review_task_id), str(form.submission_id)),
        )
        if not form_key_matches(form.idempotency_key, expected_key):
            raise BrowserDeliveryFormError(status_code=status.HTTP_400_BAD_REQUEST)
        await request_finished_set_archive(
            session,
            review_task_id=review_task_id,
            requested_by_user_id=owner.user_id,
        )
    except BrowserDeliveryFormError as error:
        return _form_error(request, principal, error.status_code, error.message)
    except HTTPException:
        return _form_error(request, principal, status.HTTP_403_FORBIDDEN, "Request denied.")
    except FinishedSetArchiveInputError as error:
        return _form_error(
            request,
            principal,
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            str(error),
        )
    except FinishedSetArchiveNotFoundError:
        return _form_error(
            request,
            principal,
            status.HTTP_404_NOT_FOUND,
            "The completed review could not be found.",
        )
    except FinishedSetArchiveConflictError as error:
        return _form_error(request, principal, status.HTTP_409_CONFLICT, str(error))
    return _redirect(review_task_id)


@router.post(
    "/review-tasks/{review_task_id}/delivery/patreon/{intent_id}:confirm-present",
    response_class=HTMLResponse,
    response_model=None,
)
async def dashboard_confirm_patreon_present(
    review_task_id: UUID,
    intent_id: UUID,
    request: Request,
    session: Session,
    principal: PublicationReader,
) -> Response:
    try:
        form = await read_patreon_confirm_present_form(request)
        owner = await _verified_owner(request, session, principal, form.csrf_token)
        await _require_review_patreon_intent(
            session,
            review_task_id=review_task_id,
            intent_id=intent_id,
        )
        expected_key = delivery_form_key(
            request.app.state.settings,
            session_id=owner.session_id,
            action="patreon-confirm-present",
            parts=(
                str(review_task_id),
                str(intent_id),
                form.expected_intent_digest,
                str(form.expected_lock_version),
            ),
        )
        if not form_key_matches(form.idempotency_key, expected_key):
            raise BrowserDeliveryFormError(status_code=status.HTTP_400_BAD_REQUEST)
        await reconcile_publication_present(
            session,
            intent_id=intent_id,
            expected_intent_digest=form.expected_intent_digest,
            expected_lock_version=form.expected_lock_version,
            remote_identifier=form.remote_identifier,
            remote_url=form.remote_url,
            evidence=form.evidence,
            attestation=form.attestation,
            actor_user_id=owner.user_id,
            actor_role=owner.role,
            idempotency_key=form.idempotency_key,
        )
    except BrowserDeliveryFormError as error:
        return _form_error(request, principal, error.status_code, error.message)
    except HTTPException as error:
        return _owner_http_error(request, principal, error)
    except OperatorDeliveryNotFoundError:
        return _form_error(
            request,
            principal,
            status.HTTP_404_NOT_FOUND,
            "The completed review could not be found.",
        )
    except (PublicationInputError, PublicationNotFoundError, PublicationConflictError):
        return _form_error(
            request,
            principal,
            status.HTTP_409_CONFLICT,
            "The Patreon outcome could not be confirmed from the current state.",
        )
    return _redirect(review_task_id)


@router.post(
    "/review-tasks/{review_task_id}/delivery/patreon/{intent_id}:confirm-absent",
    response_class=HTMLResponse,
    response_model=None,
)
async def dashboard_confirm_patreon_absent(
    review_task_id: UUID,
    intent_id: UUID,
    request: Request,
    session: Session,
    principal: PublicationReader,
) -> Response:
    try:
        form = await read_patreon_confirm_absent_form(request)
        owner = await _verified_owner(request, session, principal, form.csrf_token)
        await _require_review_patreon_intent(
            session,
            review_task_id=review_task_id,
            intent_id=intent_id,
        )
        expected_key = delivery_form_key(
            request.app.state.settings,
            session_id=owner.session_id,
            action="patreon-confirm-absent",
            parts=(
                str(review_task_id),
                str(intent_id),
                form.expected_intent_digest,
                str(form.expected_lock_version),
            ),
        )
        if not form_key_matches(form.idempotency_key, expected_key):
            raise BrowserDeliveryFormError(status_code=status.HTTP_400_BAD_REQUEST)
        await reconcile_publication_absent(
            session,
            intent_id=intent_id,
            expected_intent_digest=form.expected_intent_digest,
            expected_lock_version=form.expected_lock_version,
            evidence=form.evidence,
            attestation=form.attestation,
            actor_user_id=owner.user_id,
            actor_role=owner.role,
            idempotency_key=form.idempotency_key,
        )
    except BrowserDeliveryFormError as error:
        return _form_error(request, principal, error.status_code, error.message)
    except HTTPException as error:
        return _owner_http_error(request, principal, error)
    except OperatorDeliveryNotFoundError:
        return _form_error(
            request,
            principal,
            status.HTTP_404_NOT_FOUND,
            "The completed review could not be found.",
        )
    except (PublicationInputError, PublicationNotFoundError, PublicationConflictError):
        return _form_error(
            request,
            principal,
            status.HTTP_409_CONFLICT,
            "The Patreon outcome could not be confirmed from the current state.",
        )
    return _redirect(review_task_id)


@router.post(
    "/review-tasks/{review_task_id}/delivery:prepare-destinations",
    response_class=HTMLResponse,
    response_model=None,
)
async def dashboard_prepare_destinations(
    review_task_id: UUID,
    request: Request,
    session: Session,
    principal: PublicationReader,
) -> Response:
    try:
        form = await read_prepare_destination_form(request)
        owner = await _verified_owner(request, session, principal, form.csrf_token)
        expected_key = delivery_form_key(
            request.app.state.settings,
            session_id=owner.session_id,
            action="prepare-destinations",
            parts=(str(review_task_id), str(form.submission_id)),
        )
        if not form_key_matches(form.idempotency_key, expected_key):
            raise BrowserDeliveryFormError(status_code=status.HTTP_400_BAD_REQUEST)
        settings: Settings = request.app.state.settings
        if not settings.publishing_enabled:
            raise OperatorDeliveryConflictError("publication runtime is not configured")
        await prepare_operator_destinations(
            session,
            review_task_id=review_task_id,
            patreon_title=form.patreon_title,
            patreon_body=form.patreon_body,
            patreon_tier=form.patreon_tier,
            patreon_tags=form.patreon_tags,
            public_preview_output_id=form.public_preview_output_id,
            public_preview_attester_name=owner.display_name,
            public_preview_attested_at=form.public_preview_attested_at,
            x_text=form.x_text,
            x_credential_reference=settings.x_oauth_secret_reference,
            actor_user_id=owner.user_id,
            actor_role=owner.role,
            idempotency_key=form.idempotency_key,
        )
    except BrowserDeliveryFormError as error:
        return _form_error(request, principal, error.status_code, error.message)
    except HTTPException as error:
        return _owner_http_error(request, principal, error)
    except OperatorDeliveryInputError as error:
        return _form_error(
            request,
            principal,
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            str(error),
        )
    except OperatorDeliveryConflictError as error:
        return _form_error(
            request,
            principal,
            status.HTTP_409_CONFLICT,
            str(error),
        )
    return _redirect(review_task_id)


@router.post(
    "/review-tasks/{review_task_id}/delivery:upload-watermark",
    response_class=HTMLResponse,
    response_model=None,
)
async def dashboard_upload_watermark(
    review_task_id: UUID,
    request: Request,
    session: Session,
    principal: PublicationReader,
) -> Response:
    store = _store(request)
    if store is None:
        return _form_error(
            request,
            principal,
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Private object storage is unavailable.",
        )
    try:
        values, payload = await _read_watermark_form(request)
        csrf_token, idempotency_key, submission_id, display_name = values
        owner = await _verified_mutation_owner(request, session, principal, csrf_token)
        expected_key = delivery_form_key(
            request.app.state.settings,
            session_id=owner.session_id,
            action="upload-watermark",
            parts=(str(review_task_id), str(submission_id)),
        )
        if not form_key_matches(idempotency_key, expected_key):
            raise BrowserDeliveryFormError(status_code=status.HTTP_400_BAD_REQUEST)
        snapshot = await load_operator_delivery(session, review_task_id=review_task_id)
        await register_watermark(
            session,
            store,
            release_id=snapshot.release_id,
            display_name=display_name,
            png_bytes=payload,
            registered_by_user_id=owner.user_id,
            idempotency_key=idempotency_key,
        )
    except BrowserDeliveryFormError as error:
        return _form_error(request, principal, error.status_code, error.message)
    except HTTPException:
        return _form_error(request, principal, status.HTTP_403_FORBIDDEN, "Request denied.")
    except (
        WatermarkInputError,
        WatermarkNotFoundError,
        WatermarkConflictError,
        WatermarkStorageError,
        OperatorDeliveryNotFoundError,
    ):
        return _form_error(
            request,
            principal,
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "The watermark must be a transparent PNG no larger than 4 MiB.",
        )
    return _redirect(review_task_id)


@router.post(
    "/review-tasks/{review_task_id}/delivery/patreon/{intent_id}:download",
    response_class=HTMLResponse,
    response_model=None,
)
async def dashboard_download_patreon_package(
    review_task_id: UUID,
    intent_id: UUID,
    request: Request,
    session: Session,
    principal: PublicationReader,
) -> Response:
    store = _store(request)
    if store is None:
        return _form_error(
            request,
            principal,
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Private object storage is unavailable.",
        )
    try:
        form = await read_package_download_form(request)
        owner = await _verified_mutation_owner(request, session, principal, form.csrf_token)
        await _require_review_patreon_intent(
            session,
            review_task_id=review_task_id,
            intent_id=intent_id,
        )
        result = await presign_patreon_package_download(
            session,
            store,
            intent_id=intent_id,
            expected_intent_digest=form.expected_intent_digest,
            expected_lock_version=form.expected_lock_version,
            actor_user_id=owner.user_id,
            actor_role=owner.role,
            expires_in_seconds=300,
            part_number=form.part_number,
        )
    except BrowserDeliveryFormError as error:
        return _form_error(request, principal, error.status_code, error.message)
    except HTTPException:
        return _form_error(request, principal, status.HTTP_403_FORBIDDEN, "Request denied.")
    except (PublicationInputError, PublicationNotFoundError, PublicationConflictError):
        return _form_error(
            request,
            principal,
            status.HTTP_409_CONFLICT,
            "The Patreon package is not currently available for download.",
        )
    return _secure(
        request,
        RedirectResponse(result.url, status_code=status.HTTP_303_SEE_OTHER),
    )


@router.post(
    "/review-tasks/{review_task_id}/delivery/finished-set/{archive_id}:download",
    response_class=HTMLResponse,
    response_model=None,
)
async def dashboard_download_finished_set_package(
    review_task_id: UUID,
    archive_id: UUID,
    request: Request,
    session: Session,
    principal: PublicationReader,
) -> Response:
    store = _store(request)
    if store is None:
        return _form_error(
            request,
            principal,
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Private object storage is unavailable.",
        )
    try:
        form = await read_finished_set_archive_download_form(request)
        owner = await _verified_mutation_owner(request, session, principal, form.csrf_token)
        result = await presign_finished_set_archive_part(
            session,
            store,
            review_task_id=review_task_id,
            archive_id=archive_id,
            actor_user_id=owner.user_id,
            actor_role=owner.role,
            expires_in_seconds=300,
            part_number=form.part_number,
        )
    except BrowserDeliveryFormError as error:
        return _form_error(request, principal, error.status_code, error.message)
    except HTTPException:
        return _form_error(request, principal, status.HTTP_403_FORBIDDEN, "Request denied.")
    except FinishedSetArchiveInputError:
        return _form_error(
            request,
            principal,
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "The finished ranked-set download request is invalid.",
        )
    except FinishedSetArchiveNotFoundError:
        return _form_error(
            request,
            principal,
            status.HTTP_404_NOT_FOUND,
            "The finished ranked-set package could not be found.",
        )
    except FinishedSetArchiveConflictError:
        return _form_error(
            request,
            principal,
            status.HTTP_409_CONFLICT,
            "The finished ranked-set package is not currently available for download.",
        )
    except ObjectStoreError:
        return _form_error(
            request,
            principal,
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Private storage is temporarily unavailable. Try the ZIP download again.",
        )
    return _secure(
        request,
        RedirectResponse(result.url, status_code=status.HTTP_303_SEE_OTHER),
    )


async def _require_review_patreon_intent(
    session: AsyncSession,
    *,
    review_task_id: UUID,
    intent_id: UUID,
) -> None:
    snapshot = await load_operator_delivery(session, review_task_id=review_task_id)
    if not any(
        destination.key == "patreon" and destination.intent_id == intent_id
        for destination in snapshot.destinations
    ):
        raise PublicationConflictError("Patreon intent does not belong to this review")


async def _verified_owner(
    request: Request,
    session: AsyncSession,
    principal: AuthenticatedPrincipal,
    csrf_token: str,
) -> AuthenticatedPrincipal:
    if principal.role != AdminRole.OWNER:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
    owner = await require_publication_owner(request, session, csrf_header=csrf_token)
    settings: Settings = request.app.state.settings
    if not settings.auth_enabled and not hmac.compare_digest(
        csrf_token,
        delivery_csrf_token(settings, session_id=owner.session_id),
    ):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
    return owner


async def _verified_mutation_owner(
    request: Request,
    session: AsyncSession,
    principal: AuthenticatedPrincipal,
    csrf_token: str,
) -> AuthenticatedPrincipal:
    if principal.role != AdminRole.OWNER:
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


def _owner_http_error(
    request: Request,
    principal: AuthenticatedPrincipal,
    error: HTTPException,
) -> Response:
    if error.status_code == status.HTTP_401_UNAUTHORIZED:
        return _form_error(
            request,
            principal,
            status.HTTP_401_UNAUTHORIZED,
            "Confirm your password and authentication code, then retry this action.",
        )
    return _form_error(request, principal, status.HTTP_403_FORBIDDEN, "Request denied.")


def _csrf_token(
    request: Request,
    principal: AuthenticatedPrincipal,
) -> str:
    settings: Settings = request.app.state.settings
    if not settings.auth_enabled:
        return delivery_csrf_token(settings, session_id=principal.session_id)
    token = request.cookies.get(settings.auth_csrf_cookie_name)
    authentication_service(request).validate_csrf(
        principal,
        cookie_token=token,
        header_token=token,
    )
    if token is None:
        raise CsrfValidationError
    return token


async def _read_watermark_form(
    request: Request,
) -> tuple[tuple[str, str, UUID, str], bytes]:
    content_type = request.headers.get("content-type", "")
    if content_type.partition(";")[0].strip().lower() != "multipart/form-data":
        raise BrowserDeliveryFormError(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
        )
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            declared_length = int(content_length)
            if declared_length < 0:
                raise BrowserDeliveryFormError(status_code=status.HTTP_400_BAD_REQUEST)
            if declared_length > _MAX_MULTIPART_BYTES:
                raise BrowserDeliveryFormError(
                    status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                )
        except ValueError:
            raise BrowserDeliveryFormError(status_code=status.HTTP_400_BAD_REQUEST) from None
    try:
        form = await request.form(
            max_files=1,
            max_fields=4,
            max_part_size=MAX_WATERMARK_BYTES + 1,
        )
    except (HTTPException, MultiPartException, ValueError):
        raise BrowserDeliveryFormError(status_code=status.HTTP_400_BAD_REQUEST) from None
    try:
        items = list(form.multi_items())
        if len(items) != 5 or {key for key, _ in items} != {
            "csrf_token",
            "idempotency_key",
            "submission_id",
            "display_name",
            "file",
        }:
            raise BrowserDeliveryFormError(status_code=status.HTTP_400_BAD_REQUEST)
        file = form.get("file")
        if not isinstance(file, UploadFile) or file.content_type != "image/png":
            raise BrowserDeliveryFormError(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            )
        csrf_token = _form_text(form.get("csrf_token"), 200)
        idempotency_key = _form_text(form.get("idempotency_key"), 200)
        display_name = _form_text(form.get("display_name"), 100)
        try:
            submission_id = UUID(_form_text(form.get("submission_id"), 36))
        except ValueError:
            raise BrowserDeliveryFormError(status_code=status.HTTP_400_BAD_REQUEST) from None
        payload = await file.read(MAX_WATERMARK_BYTES + 1)
        if not payload or len(payload) > MAX_WATERMARK_BYTES:
            raise BrowserDeliveryFormError(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            )
        return (csrf_token, idempotency_key, submission_id, display_name), payload
    finally:
        await form.close()


def _form_text(value: object, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise BrowserDeliveryFormError(status_code=status.HTTP_400_BAD_REQUEST)
    return value


async def _preview_views(
    request: Request,
    outputs: tuple[DeliveryOutput, ...],
) -> tuple[PreviewView, ...]:
    store = _store(request)
    if store is None:
        return ()
    settings: Settings = request.app.state.settings
    previews: list[PreviewView] = []
    for output in outputs:
        previews.append(
            PreviewView(
                output_id=output.output_id,
                display_order=output.display_order,
                width=output.width,
                height=output.height,
                url=await store.presign_download(
                    key=output.object_key,
                    version_id=output.object_version_id,
                    expires_in=min(settings.storage_presign_ttl_seconds, 900),
                ),
            )
        )
    return tuple(previews)


def _delivery_progress_payload(
    snapshot: OperatorDeliverySnapshot,
    *,
    finished_set_archive: FinishedSetArchiveSnapshot | None,
    mega_enabled: bool = False,
) -> dict[str, object]:
    progress = snapshot.progress
    if progress.outputs_ready:
        output_state = "ready"
    elif progress.terminal_failures:
        output_state = "failed"
    elif progress.stalled:
        output_state = "stalled"
    elif progress.planned:
        output_state = "rendering"
    else:
        output_state = "not_started"

    parts = (
        tuple(sorted(finished_set_archive.parts, key=lambda part: part.part_number))
        if finished_set_archive is not None
        else ()
    )
    archive_detail: str | None = None
    if _finished_set_archive_parts_ready(finished_set_archive):
        archive_state = "ready"
    elif finished_set_archive is None:
        archive_state = "not_started"
        archive_detail = "Waiting for automatic ZIP preparation or an explicit request."
    elif finished_set_archive.state == FinishedSetArchiveState.FAILED:
        archive_state = "failed"
        archive_detail = finished_set_archive.last_error_detail
    elif finished_set_archive.state in {
        FinishedSetArchiveState.PENDING,
        FinishedSetArchiveState.PROCESSING,
        FinishedSetArchiveState.RETRY_WAIT,
    }:
        archive_state = "preparing"
        if finished_set_archive.state == FinishedSetArchiveState.RETRY_WAIT:
            archive_detail = "ZIP creation will retry automatically."
    else:
        archive_state = "failed"
        archive_detail = "The finished-set archive record is incomplete."

    mega = next(destination for destination in snapshot.destinations if destination.key == "mega")
    mega_active = mega.state == "running" or (mega_enabled and mega.state == "queued")
    payload: dict[str, object] = {
        "schema": "delivery-progress/v1",
        "review_task_id": str(snapshot.review_task_id),
        "outputs": {
            "state": output_state,
            "full_outputs_ready": progress.full_outputs_ready,
            "planned": progress.planned,
            "total_jobs": progress.total_jobs,
            "requested": progress.requested,
            "running": progress.running,
            "retrying": progress.retrying,
            "succeeded": progress.succeeded,
            "failed": progress.failed,
            "active_jobs": progress.active_jobs,
            "expected_full_outputs": progress.expected_full_outputs,
            "ready_full_outputs": progress.ready_full_outputs,
            "expected_x_teasers": progress.expected_x_teasers,
            "ready_x_teasers": progress.ready_x_teasers,
        },
        "archive": {
            "state": archive_state,
            "detail": archive_detail,
            "archive_id": (
                str(finished_set_archive.archive_id) if finished_set_archive is not None else None
            ),
            "worker_state": (
                finished_set_archive.state.value if finished_set_archive is not None else None
            ),
            "part_count": len(parts),
            "parts": [
                {
                    "part_number": part.part_number,
                    "part_count": part.part_count,
                    "first_ordinal": part.first_ordinal,
                    "last_ordinal": part.last_ordinal,
                }
                for part in parts
            ],
        },
        "mega": {
            "state": mega.state,
            "active": mega_active,
            "detail": mega.detail,
            "completed_items": mega.completed_items,
            "total_items": mega.total_items,
            "remote_path": mega.remote_path,
        },
        "poll_after_ms": (
            3000
            if output_state == "rendering"
            or archive_state == "preparing"
            or (archive_state == "not_started" and progress.full_outputs_ready)
            or mega_active
            else None
        ),
    }
    return payload


def _finished_set_archive_parts_ready(
    archive: FinishedSetArchiveSnapshot | None,
) -> bool:
    if (
        archive is None
        or archive.state != FinishedSetArchiveState.READY
        or archive.part_count is None
    ):
        return False
    part_count = archive.part_count
    parts = tuple(sorted(archive.parts, key=lambda part: part.part_number))
    return (
        archive.selection_count > 0
        and archive.manifest_sha256 is not None
        and part_count == len(parts)
        and part_count > 0
        and tuple(part.part_number for part in parts) == tuple(range(1, part_count + 1))
        and all(part.part_count == part_count for part in parts)
        and all(part.manifest_sha256 == archive.manifest_sha256 for part in parts)
        and parts[0].first_ordinal == 1
        and parts[-1].last_ordinal == archive.selection_count
        and all(
            current.last_ordinal + 1 == following.first_ordinal
            for current, following in pairwise(parts)
        )
    )


def _can_render_destination_form(
    snapshot: OperatorDeliverySnapshot,
    *,
    settings: Settings,
) -> bool:
    return (
        settings.publishing_enabled
        and snapshot.publishing_guard_enabled
        and (snapshot.x_selected_count == 0 or settings.x_oauth_secret_reference is not None)
        and snapshot.progress.ready_for_destinations
        and (not snapshot.destinations_prepared or snapshot.destinations_need_retry)
    )


def _store(request: Request) -> ObjectStore | None:
    return cast(ObjectStore | None, getattr(request.app.state, "object_store", None))


def _redirect(review_task_id: UUID) -> Response:
    return RedirectResponse(
        f"/dashboard/review-tasks/{review_task_id}/delivery",
        status_code=status.HTTP_303_SEE_OTHER,
    )


def _form_error(
    request: Request,
    principal: AuthenticatedPrincipal,
    status_code: int,
    message: str,
) -> Response:
    return _error(
        request,
        principal,
        status_code=status_code,
        heading="Delivery action was not completed",
        message=message,
    )


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
