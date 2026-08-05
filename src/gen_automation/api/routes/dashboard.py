import hmac
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from gen_automation.api.browser_review_forms import (
    BrowserReviewFormError,
    form_key_matches,
    read_anatomy_feedback_form,
    read_bulk_action_form,
    read_create_form,
    read_decision_form,
    read_inspection_form,
    read_transition_form,
    read_x_selection_form,
    review_form_idempotency_key,
)
from gen_automation.api.security import (
    RawMasterReader,
    ReleaseReader,
    ReviewReader,
    authentication_service,
    require_same_origin,
)
from gen_automation.api.semantic_feedback import semantic_feedback_target_belongs_to_review
from gen_automation.config import Settings
from gen_automation.db.session import get_session
from gen_automation.domain.enums import (
    AdminRole,
    ReviewBulkAction,
    ReviewDecisionValue,
    ReviewTaskState,
    SemanticEnforcementMode,
    SemanticIssueCode,
)
from gen_automation.middleware import content_security_policy
from gen_automation.services.assets import (
    AssetConflictError,
    AssetNotFoundError,
    AssetStorageUnavailableError,
    presign_raw_master_access,
)
from gen_automation.services.authentication import (
    AuthenticatedPrincipal,
    CsrfValidationError,
)
from gen_automation.services.generation_details import (
    GenerationDetailsNotFoundError,
    load_generation_details,
    unavailable_generation_details,
)
from gen_automation.services.ranked_dashboard import (
    RankedMaster,
    RankedReleaseNotFoundError,
    RankingIntegrityError,
    RankingStorageError,
    RankingUnavailableError,
    find_review_task_id,
    list_dashboard_releases,
    load_current_completed_scoring_run_id,
    load_ranked_release,
    load_ranked_scoring_run,
    load_review_task_navigation,
)
from gen_automation.services.review import (
    SEMANTIC_SEVERE_OVERRIDE_REASON_CODE,
    CurrentAssetDecision,
    ReviewConflictError,
    ReviewInputError,
    ReviewNotFoundError,
    ReviewSummary,
    append_review_decision,
    apply_bulk_review_action,
    create_review_task,
    get_review_summary,
    set_review_x_selection,
    transition_review_task,
)
from gen_automation.services.review_inspections import (
    load_review_inspected_asset_ids,
    record_review_inspections,
)
from gen_automation.services.semantic_anatomy import (
    SemanticAssessmentProfile,
    SemanticReviewAssessment,
    load_semantic_review_assessments,
)
from gen_automation.services.semantic_feedback import (
    DEFAULT_CALIBRATION_MINIMUM_SAMPLES,
    SemanticAnatomyFeedbackResult,
    SemanticCalibrationArtifactResult,
    SemanticFeedbackAssessmentNotReadyError,
    SemanticFeedbackConflictError,
    SemanticFeedbackNotFoundError,
    SemanticFeedbackValidationError,
    load_effective_semantic_threshold_micros,
    load_latest_semantic_calibration_artifact,
    load_semantic_anatomy_feedback,
    record_semantic_anatomy_feedback,
    refresh_semantic_calibration_artifact,
)
from gen_automation.services.semantic_review_learning import ANATOMY_REASON_CODES
from gen_automation.storage.base import ObjectStore, ObjectStoreError

router = APIRouter(prefix="/dashboard", tags=["dashboard"], include_in_schema=False)
templates = Jinja2Templates(directory=str(Path(__file__).parents[2] / "templates"))
Session = Annotated[AsyncSession, Depends(get_session)]


@dataclass(frozen=True, slots=True)
class ReviewAssetView:
    master: RankedMaster
    current: CurrentAssetDecision
    semantic: SemanticReviewAssessment | None
    semantic_feedback: SemanticAnatomyFeedbackResult | None
    semantic_flagged: bool
    ai_excluded: bool
    inspected: bool
    idempotency_key: str | None
    x_selection_idempotency_key: str | None


@dataclass(frozen=True, slots=True)
class ReviewAssetGroups:
    regular: tuple[ReviewAssetView, ...]
    ai_excluded: tuple[ReviewAssetView, ...]

    @property
    def ordered(self) -> tuple[ReviewAssetView, ...]:
        return self.regular + self.ai_excluded


class BrowserReviewSecurityError(Exception):
    """A browser review request failed its origin or CSRF boundary."""


def _secure_response(request: Request, response: Response) -> Response:
    response.headers["Cache-Control"] = "private, no-store"
    settings: Settings = request.app.state.settings
    response.headers["Content-Security-Policy"] = content_security_policy(settings.environment)
    return response


@router.get(
    "/assets/{asset_id}/generation-details",
    response_class=JSONResponse,
    response_model=None,
    name="dashboard_asset_generation_details",
)
async def dashboard_asset_generation_details(
    asset_id: UUID,
    request: Request,
    session: Session,
    _principal: RawMasterReader,
) -> Response:
    try:
        details = await load_generation_details(session, asset_id=asset_id)
    except GenerationDetailsNotFoundError:
        return _secure_response(
            request,
            JSONResponse(
                status_code=status.HTTP_404_NOT_FOUND,
                content=unavailable_generation_details(),
            ),
        )
    return _secure_response(
        request,
        JSONResponse(content=details or unavailable_generation_details()),
    )


@router.get(
    "/assets/{asset_id}/view",
    response_class=RedirectResponse,
    response_model=None,
    name="dashboard_asset_view",
)
async def dashboard_asset_view(
    asset_id: UUID,
    request: Request,
    session: Session,
    _principal: RawMasterReader,
) -> Response:
    return await _asset_access_redirect(
        asset_id,
        request=request,
        session=session,
        as_attachment=False,
    )


@router.get(
    "/assets/{asset_id}/download",
    response_class=RedirectResponse,
    response_model=None,
    name="dashboard_asset_download",
)
async def dashboard_asset_download(
    asset_id: UUID,
    request: Request,
    session: Session,
    _principal: RawMasterReader,
) -> Response:
    return await _asset_access_redirect(
        asset_id,
        request=request,
        session=session,
        as_attachment=True,
    )


async def _asset_access_redirect(
    asset_id: UUID,
    *,
    request: Request,
    session: AsyncSession,
    as_attachment: bool,
) -> Response:
    store = _object_store(request)
    if store is None:
        return _secure_response(
            request,
            JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={"detail": "private storage is unavailable"},
            ),
        )
    settings: Settings = request.app.state.settings
    try:
        url = await presign_raw_master_access(
            session,
            store,
            asset_id=asset_id,
            expires_in=min(settings.storage_presign_ttl_seconds, 900),
            as_attachment=as_attachment,
        )
    except AssetNotFoundError:
        return _secure_response(
            request,
            JSONResponse(
                status_code=status.HTTP_404_NOT_FOUND,
                content={"detail": "raw master not found"},
            ),
        )
    except AssetConflictError:
        return _secure_response(
            request,
            JSONResponse(
                status_code=status.HTTP_409_CONFLICT,
                content={"detail": "raw master is not available"},
            ),
        )
    except (AssetStorageUnavailableError, ObjectStoreError):
        return _secure_response(
            request,
            JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={"detail": "raw master storage is unavailable"},
            ),
        )
    return _secure_response(
        request,
        RedirectResponse(url=url, status_code=status.HTTP_307_TEMPORARY_REDIRECT),
    )


@router.get("", response_class=HTMLResponse, name="dashboard_index")
async def dashboard_index(
    request: Request,
    session: Session,
    principal: ReleaseReader,
) -> Response:
    releases = await list_dashboard_releases(session)
    return _secure_response(
        request,
        templates.TemplateResponse(
            request=request,
            name="dashboard/index.html",
            context={
                "page_title": "Ranked releases",
                "principal": principal,
                "releases": releases,
            },
        ),
    )


@router.get(
    "/releases/{release_id}",
    response_class=HTMLResponse,
    name="dashboard_release_detail",
)
async def dashboard_release_detail(
    release_id: UUID,
    request: Request,
    session: Session,
    principal: RawMasterReader,
) -> Response:
    store = _object_store(request)
    if store is None:
        return _error_response(
            request,
            principal=principal,
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            heading="Private storage is unavailable",
            message="Raw-master links cannot be created until private object storage is ready.",
        )
    settings: Settings = request.app.state.settings
    try:
        release = await load_ranked_release(
            session,
            store=store,
            release_id=release_id,
            expires_in=min(settings.storage_presign_ttl_seconds, 900),
        )
        review_task_id = await find_review_task_id(
            session,
            scoring_run_id=release.scoring_run_id,
        )
        csrf_token = _form_csrf_token(request, principal)
    except RankedReleaseNotFoundError:
        return _error_response(
            request,
            principal=principal,
            status_code=status.HTTP_404_NOT_FOUND,
            heading="Release not found",
            message="The requested release does not exist.",
        )
    except RankingUnavailableError:
        return _error_response(
            request,
            principal=principal,
            status_code=status.HTTP_409_CONFLICT,
            heading="Ranking not ready",
            message="The current release version has no completed ranking yet.",
        )
    except RankingIntegrityError:
        return _error_response(
            request,
            principal=principal,
            status_code=status.HTTP_409_CONFLICT,
            heading="Ranking needs attention",
            message="The completed ranking is incomplete or inconsistent.",
        )
    except RankingStorageError:
        return _error_response(
            request,
            principal=principal,
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            heading="Raw-master links are unavailable",
            message="Private, exact-version links could not be created. Try again shortly.",
        )
    except BrowserReviewSecurityError:
        return _error_response(
            request,
            principal=principal,
            status_code=status.HTTP_403_FORBIDDEN,
            heading="Session verification failed",
            message="Sign in again before opening a review.",
        )

    create_idempotency_key = review_form_idempotency_key(
        settings,
        session_id=principal.session_id,
        action="create",
        parts=(str(release.release_id), str(release.scoring_run_id)),
    )
    return _secure_response(
        request,
        templates.TemplateResponse(
            request=request,
            name="dashboard/release_detail.html",
            context={
                "page_title": release.release_title,
                "principal": principal,
                "release": release,
                "review_task_id": review_task_id,
                "csrf_token": csrf_token,
                "create_idempotency_key": create_idempotency_key,
            },
        ),
    )


@router.post(
    "/releases/{release_id}/review-tasks",
    response_class=HTMLResponse,
    response_model=None,
    name="dashboard_create_review_task",
)
async def dashboard_create_review_task(
    release_id: UUID,
    request: Request,
    session: Session,
    principal: ReviewReader,
) -> Response:
    origin_error = _origin_error_response(request, principal)
    if origin_error is not None:
        return origin_error
    try:
        csrf_token, idempotency_key = await read_create_form(request)
    except BrowserReviewFormError as error:
        return _invalid_form_response(request, principal, error.status_code)
    if not _valid_form_csrf(request, principal, csrf_token):
        return _security_error_response(request, principal)

    settings: Settings = request.app.state.settings
    try:
        scoring_run_id = await load_current_completed_scoring_run_id(
            session,
            release_id=release_id,
        )
    except RankedReleaseNotFoundError:
        return _review_action_error(
            request,
            principal,
            status_code=status.HTTP_404_NOT_FOUND,
            heading="Review could not be opened",
            message="The requested review resource was not found.",
        )
    except RankingUnavailableError:
        return _review_action_error(
            request,
            principal,
            status_code=status.HTTP_409_CONFLICT,
            heading="Review could not be opened",
            message="A completed ranking is required before review can begin.",
        )
    expected_key = review_form_idempotency_key(
        settings,
        session_id=principal.session_id,
        action="create",
        parts=(str(release_id), str(scoring_run_id)),
    )
    if not form_key_matches(idempotency_key, expected_key):
        return _invalid_form_response(
            request,
            principal,
            status.HTTP_400_BAD_REQUEST,
        )
    try:
        result = await create_review_task(
            session,
            scoring_run_id=scoring_run_id,
            created_by_user_id=principal.user_id,
            idempotency_key=idempotency_key,
            default_accept_ranked_assets=True,
        )
    except (ReviewInputError, ReviewNotFoundError, ReviewConflictError) as error:
        return _service_error_response(request, principal, error)
    return _review_redirect(request, result.task_id)


@router.get(
    "/review-tasks/{review_task_id}",
    response_class=HTMLResponse,
    name="dashboard_review_task",
)
async def dashboard_review_task(
    review_task_id: UUID,
    request: Request,
    session: Session,
    principal: ReviewReader,
) -> Response:
    store = _object_store(request)
    if store is None:
        return _error_response(
            request,
            principal=principal,
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            heading="Private storage is unavailable",
            message="Review images cannot be displayed until private storage is ready.",
        )
    settings: Settings = request.app.state.settings
    semantic_profile = _configured_semantic_profile_sha256(settings)
    semantic_mode = _semantic_mode(settings)
    try:
        active_semantic_calibration = (
            await load_latest_semantic_calibration_artifact(
                session,
                profile_sha256=semantic_profile,
            )
            if semantic_profile is not None
            else None
        )
        semantic_threshold = await _effective_semantic_threshold(
            session,
            settings=settings,
            profile_sha256=semantic_profile,
        )
        navigation = await load_review_task_navigation(
            session,
            review_task_id=review_task_id,
        )
        summary = await get_review_summary(
            session,
            review_task_id=review_task_id,
            semantic_profile_sha256=semantic_profile,
            semantic_severe_confidence_micros=semantic_threshold,
            semantic_enforcement_mode=settings.semantic_anatomy_mode,
        )
        release = await load_ranked_scoring_run(
            session,
            store=store,
            scoring_run_id=navigation.scoring_run_id,
            expires_in=min(settings.storage_presign_ttl_seconds, 900),
        )
        semantic_assessments = await load_semantic_review_assessments(
            session,
            scoring_run_id=navigation.scoring_run_id,
            profile_sha256=semantic_profile,
        )
        semantic_feedback: dict[UUID, SemanticAnatomyFeedbackResult] = {}
        inspected_asset_ids = await load_review_inspected_asset_ids(
            session,
            review_task_id=review_task_id,
            inspected_by_user_id=principal.user_id,
        )
        semantic_calibration: SemanticCalibrationArtifactResult | None = (
            active_semantic_calibration if principal.role == AdminRole.OWNER else None
        )
        if principal.role == AdminRole.OWNER:
            semantic_feedback = await load_semantic_anatomy_feedback(
                session,
                assessment_ids=tuple(
                    assessment.assessment_id for assessment in semantic_assessments.values()
                ),
                user_id=principal.user_id,
            )
        csrf_token = (
            _form_csrf_token(request, principal)
            if summary.state == ReviewTaskState.OPEN
            or (summary.state == ReviewTaskState.COMPLETED and principal.role == AdminRole.OWNER)
            else None
        )
    except (RankedReleaseNotFoundError, ReviewNotFoundError):
        return _error_response(
            request,
            principal=principal,
            status_code=status.HTTP_404_NOT_FOUND,
            heading="Review not found",
            message="The requested review task does not exist.",
        )
    except (RankingUnavailableError, RankingIntegrityError, ReviewConflictError):
        return _error_response(
            request,
            principal=principal,
            status_code=status.HTTP_409_CONFLICT,
            heading="Review needs attention",
            message="The frozen review snapshot is incomplete or inconsistent.",
        )
    except RankingStorageError:
        return _error_response(
            request,
            principal=principal,
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            heading="Review images are unavailable",
            message="Private image links could not be created. Try again shortly.",
        )
    except BrowserReviewSecurityError:
        return _error_response(
            request,
            principal=principal,
            status_code=status.HTTP_403_FORBIDDEN,
            heading="Session verification failed",
            message="Sign in again before changing this review.",
        )

    try:
        assets = _review_assets(
            settings,
            principal=principal,
            review_task_id=review_task_id,
            summary=summary,
            masters=release.assets,
            semantic_assessments=semantic_assessments,
            semantic_feedback=semantic_feedback,
            inspected_asset_ids=inspected_asset_ids,
            semantic_mode=semantic_mode,
            severe_confidence_micros=semantic_threshold,
        )
    except RankingIntegrityError:
        return _error_response(
            request,
            principal=principal,
            status_code=status.HTTP_409_CONFLICT,
            heading="Review needs attention",
            message="The frozen review snapshot is incomplete or inconsistent.",
        )
    complete_idempotency_key = None
    cancel_idempotency_key = None
    bulk_action_idempotency_key = None
    inspection_idempotency_key = None
    if summary.state == ReviewTaskState.OPEN:
        complete_idempotency_key = review_form_idempotency_key(
            settings,
            session_id=principal.session_id,
            action="complete",
            parts=(str(review_task_id), str(summary.lock_version)),
        )
        cancel_idempotency_key = review_form_idempotency_key(
            settings,
            session_id=principal.session_id,
            action="cancel",
            parts=(str(review_task_id), str(summary.lock_version)),
        )
        bulk_action_idempotency_key = review_form_idempotency_key(
            settings,
            session_id=principal.session_id,
            action="bulk-action",
            parts=(str(review_task_id), str(summary.lock_version)),
        )
        inspection_idempotency_key = review_form_idempotency_key(
            settings,
            session_id=principal.session_id,
            action="inspection-token",
            parts=(str(review_task_id),),
        )
    return _secure_response(
        request,
        templates.TemplateResponse(
            request=request,
            name="dashboard/review_task.html",
            context={
                "page_title": f"Review {release.release_title}",
                "principal": principal,
                "release": release,
                "navigation": navigation,
                "summary": summary,
                "assets": assets.ordered,
                "ai_excluded_count": len(assets.ai_excluded),
                "semantic_flagged_count": sum(asset.semantic_flagged for asset in assets.ordered),
                "semantic_override_reason_code": (SEMANTIC_SEVERE_OVERRIDE_REASON_CODE),
                "semantic_issue_codes": tuple(SemanticIssueCode),
                "anatomy_reason_codes": ANATOMY_REASON_CODES,
                "semantic_mode": semantic_mode,
                "semantic_calibration": semantic_calibration,
                "semantic_effective_threshold_micros": semantic_threshold,
                "semantic_calibration_minimum_samples": (DEFAULT_CALIBRATION_MINIMUM_SAMPLES),
                "csrf_token": csrf_token,
                "complete_idempotency_key": complete_idempotency_key,
                "cancel_idempotency_key": cancel_idempotency_key,
                "bulk_action_idempotency_key": bulk_action_idempotency_key,
                "inspection_idempotency_key": inspection_idempotency_key,
                "inspection_endpoint": str(
                    request.url_for(
                        "dashboard_review_inspections",
                        review_task_id=review_task_id,
                    )
                ),
                "can_complete": (
                    summary.state == ReviewTaskState.OPEN
                    and 1 <= summary.accepted_count <= summary.desired_accepted_count
                    and all(
                        not asset.selected_for_x or asset.decision == ReviewDecisionValue.ACCEPT
                        for asset in summary.assets
                    )
                    and summary.semantic_gate.completion_ready
                ),
            },
        ),
    )


@router.post(
    "/review-tasks/{review_task_id}/decisions",
    response_class=HTMLResponse,
    response_model=None,
    name="dashboard_review_decision",
)
async def dashboard_review_decision(
    review_task_id: UUID,
    request: Request,
    session: Session,
    principal: ReviewReader,
) -> Response:
    origin_error = _origin_error_response(request, principal)
    if origin_error is not None:
        return origin_error
    try:
        form = await read_decision_form(request)
    except BrowserReviewFormError as error:
        return _invalid_form_response(request, principal, error.status_code)
    if not _valid_form_csrf(request, principal, form.csrf_token):
        return _security_error_response(request, principal)

    settings: Settings = request.app.state.settings
    semantic_profile = _configured_semantic_profile_sha256(settings)
    semantic_threshold = await _effective_semantic_threshold(
        session,
        settings=settings,
        profile_sha256=semantic_profile,
    )
    expected_key = review_form_idempotency_key(
        settings,
        session_id=principal.session_id,
        action="decision",
        parts=(
            str(review_task_id),
            str(form.asset_id),
            str(form.expected_lock_version),
        ),
    )
    if not form_key_matches(form.idempotency_key, expected_key):
        return _invalid_form_response(
            request,
            principal,
            status.HTTP_400_BAD_REQUEST,
        )
    try:
        await append_review_decision(
            session,
            review_task_id=review_task_id,
            asset_id=form.asset_id,
            decision=form.decision,
            decided_by_user_id=principal.user_id,
            expected_lock_version=form.expected_lock_version,
            idempotency_key=form.idempotency_key,
            reason_code=form.reason_code,
            note=form.note,
            semantic_profile_sha256=semantic_profile,
            semantic_severe_confidence_micros=semantic_threshold,
            semantic_enforcement_mode=settings.semantic_anatomy_mode,
        )
    except (ReviewInputError, ReviewNotFoundError, ReviewConflictError) as error:
        return _service_error_response(request, principal, error)
    return _review_redirect(request, review_task_id)


@router.post(
    "/review-tasks/{review_task_id}/inspections",
    response_class=JSONResponse,
    response_model=None,
    name="dashboard_review_inspections",
)
async def dashboard_review_inspections(
    review_task_id: UUID,
    request: Request,
    session: Session,
    principal: ReviewReader,
) -> Response:
    try:
        require_same_origin(request)
    except HTTPException:
        return _secure_response(
            request,
            JSONResponse(
                status_code=status.HTTP_403_FORBIDDEN,
                content={"ok": False, "detail": "request verification failed"},
            ),
        )
    try:
        form = await read_inspection_form(request)
    except BrowserReviewFormError as error:
        return _secure_response(
            request,
            JSONResponse(
                status_code=error.status_code,
                content={"ok": False, "detail": "inspection form is invalid"},
            ),
        )
    if not _valid_form_csrf(request, principal, form.csrf_token):
        return _secure_response(
            request,
            JSONResponse(
                status_code=status.HTTP_403_FORBIDDEN,
                content={"ok": False, "detail": "request verification failed"},
            ),
        )
    settings: Settings = request.app.state.settings
    expected_key = review_form_idempotency_key(
        settings,
        session_id=principal.session_id,
        action="inspection-token",
        parts=(str(review_task_id),),
    )
    if not form_key_matches(form.idempotency_key, expected_key):
        return _secure_response(
            request,
            JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={"ok": False, "detail": "inspection token is invalid"},
            ),
        )
    try:
        result = await record_review_inspections(
            session,
            review_task_id=review_task_id,
            asset_ids=form.asset_ids,
            inspected_by_user_id=principal.user_id,
        )
    except (ReviewInputError, ReviewNotFoundError, ReviewConflictError) as error:
        await session.rollback()
        status_code = (
            status.HTTP_404_NOT_FOUND
            if isinstance(error, ReviewNotFoundError)
            else status.HTTP_409_CONFLICT
            if isinstance(error, ReviewConflictError)
            else status.HTTP_422_UNPROCESSABLE_CONTENT
        )
        return _secure_response(
            request,
            JSONResponse(
                status_code=status_code,
                content={"ok": False, "detail": str(error)},
            ),
        )
    return _secure_response(
        request,
        JSONResponse(
            content={
                "ok": True,
                "inspected_asset_ids": [
                    str(asset_id) for asset_id in result.inspected_asset_ids
                ],
                "created_count": result.created_count,
            }
        ),
    )


@router.post(
    "/review-tasks/{review_task_id}/anatomy-feedback",
    response_class=HTMLResponse,
    response_model=None,
    name="dashboard_anatomy_feedback",
)
async def dashboard_anatomy_feedback(
    review_task_id: UUID,
    request: Request,
    session: Session,
    principal: ReviewReader,
) -> Response:
    origin_error = _origin_error_response(request, principal)
    if origin_error is not None:
        return origin_error
    if principal.role != AdminRole.OWNER:
        return _security_error_response(request, principal)
    try:
        form = await read_anatomy_feedback_form(request)
    except BrowserReviewFormError as error:
        return _invalid_form_response(request, principal, error.status_code)
    if not _valid_form_csrf(request, principal, form.csrf_token):
        return _security_error_response(request, principal)

    settings: Settings = request.app.state.settings
    semantic_profile = _configured_semantic_profile_sha256(settings)
    if semantic_profile is None or not await semantic_feedback_target_belongs_to_review(
        session,
        review_task_id=review_task_id,
        assessment_id=form.assessment_id,
        profile_sha256=semantic_profile,
    ):
        return _review_action_error(
            request,
            principal,
            status_code=status.HTTP_404_NOT_FOUND,
            heading="Anatomy feedback was not saved",
            message="This assessment is not part of the current review.",
        )
    try:
        await record_semantic_anatomy_feedback(
            session,
            assessment_id=form.assessment_id,
            user_id=principal.user_id,
            ground_truth=form.ground_truth,
            issue_code=form.issue_code,
            note=form.note,
        )
        await refresh_semantic_calibration_artifact(
            session,
            profile_sha256=semantic_profile,
            created_by_user_id=principal.user_id,
            configured_baseline_threshold_micros=(
                settings.semantic_anatomy_severe_confidence_micros
            ),
        )
        await session.commit()
    except (
        SemanticFeedbackAssessmentNotReadyError,
        SemanticFeedbackConflictError,
        SemanticFeedbackNotFoundError,
        SemanticFeedbackValidationError,
    ) as error:
        await session.rollback()
        return _feedback_service_error_response(request, principal, error)
    return _review_redirect(request, review_task_id)


@router.post(
    "/review-tasks/{review_task_id}/bulk-actions",
    response_class=HTMLResponse,
    response_model=None,
    name="dashboard_bulk_review_action",
)
async def dashboard_bulk_review_action(
    review_task_id: UUID,
    request: Request,
    session: Session,
    principal: ReviewReader,
) -> Response:
    origin_error = _origin_error_response(request, principal)
    if origin_error is not None:
        return origin_error
    try:
        form = await read_bulk_action_form(request)
    except BrowserReviewFormError as error:
        return _invalid_form_response(request, principal, error.status_code)
    if not _valid_form_csrf(request, principal, form.csrf_token):
        return _security_error_response(request, principal)
    if form.action in {ReviewBulkAction.X_ADD, ReviewBulkAction.X_REMOVE} and (
        principal.role != AdminRole.OWNER
    ):
        return _security_error_response(request, principal)

    settings: Settings = request.app.state.settings
    expected_key = review_form_idempotency_key(
        settings,
        session_id=principal.session_id,
        action="bulk-action",
        parts=(str(review_task_id), str(form.expected_lock_version)),
    )
    if not form_key_matches(form.idempotency_key, expected_key):
        return _invalid_form_response(request, principal, status.HTTP_400_BAD_REQUEST)
    semantic_profile = _configured_semantic_profile_sha256(settings)
    semantic_threshold = await _effective_semantic_threshold(
        session,
        settings=settings,
        profile_sha256=semantic_profile,
    )
    try:
        await apply_bulk_review_action(
            session,
            review_task_id=review_task_id,
            asset_ids=form.asset_ids,
            action=form.action,
            changed_by_user_id=principal.user_id,
            expected_lock_version=form.expected_lock_version,
            idempotency_key=form.idempotency_key,
            reason_code=form.reason_code,
            note=form.note,
            semantic_profile_sha256=semantic_profile,
            semantic_severe_confidence_micros=semantic_threshold,
            semantic_enforcement_mode=settings.semantic_anatomy_mode,
        )
    except (ReviewInputError, ReviewNotFoundError, ReviewConflictError) as error:
        return _service_error_response(request, principal, error)
    return _review_redirect(request, review_task_id)


@router.post(
    "/review-tasks/{review_task_id}/x-selections",
    response_class=HTMLResponse,
    response_model=None,
    name="dashboard_review_x_selection",
)
async def dashboard_review_x_selection(
    review_task_id: UUID,
    request: Request,
    session: Session,
    principal: ReviewReader,
) -> Response:
    origin_error = _origin_error_response(request, principal)
    if origin_error is not None:
        return origin_error
    if principal.role != AdminRole.OWNER:
        return _security_error_response(request, principal)
    try:
        form = await read_x_selection_form(request)
    except BrowserReviewFormError as error:
        return _invalid_form_response(request, principal, error.status_code)
    if not _valid_form_csrf(request, principal, form.csrf_token):
        return _security_error_response(request, principal)

    settings: Settings = request.app.state.settings
    expected_key = review_form_idempotency_key(
        settings,
        session_id=principal.session_id,
        action="x-selection",
        parts=(
            str(review_task_id),
            str(form.asset_id),
            str(form.selected).lower(),
            str(form.expected_lock_version),
        ),
    )
    if not form_key_matches(form.idempotency_key, expected_key):
        return _invalid_form_response(
            request,
            principal,
            status.HTTP_400_BAD_REQUEST,
        )
    try:
        await set_review_x_selection(
            session,
            review_task_id=review_task_id,
            asset_id=form.asset_id,
            selected=form.selected,
            selected_by_user_id=principal.user_id,
            expected_lock_version=form.expected_lock_version,
        )
    except (ReviewInputError, ReviewNotFoundError, ReviewConflictError) as error:
        return _service_error_response(request, principal, error)
    return _review_redirect(request, review_task_id)


@router.post(
    "/review-tasks/{review_task_id}:complete",
    response_class=HTMLResponse,
    response_model=None,
    name="dashboard_complete_review_task",
)
async def dashboard_complete_review_task(
    review_task_id: UUID,
    request: Request,
    session: Session,
    principal: ReviewReader,
) -> Response:
    return await _dashboard_transition(
        review_task_id=review_task_id,
        target_state=ReviewTaskState.COMPLETED,
        request=request,
        session=session,
        principal=principal,
    )


@router.post(
    "/review-tasks/{review_task_id}:cancel",
    response_class=HTMLResponse,
    response_model=None,
    name="dashboard_cancel_review_task",
)
async def dashboard_cancel_review_task(
    review_task_id: UUID,
    request: Request,
    session: Session,
    principal: ReviewReader,
) -> Response:
    return await _dashboard_transition(
        review_task_id=review_task_id,
        target_state=ReviewTaskState.CANCELLED,
        request=request,
        session=session,
        principal=principal,
    )


async def _dashboard_transition(
    *,
    review_task_id: UUID,
    target_state: ReviewTaskState,
    request: Request,
    session: AsyncSession,
    principal: AuthenticatedPrincipal,
) -> Response:
    origin_error = _origin_error_response(request, principal)
    if origin_error is not None:
        return origin_error
    try:
        form = await read_transition_form(request)
    except BrowserReviewFormError as error:
        return _invalid_form_response(request, principal, error.status_code)
    if not _valid_form_csrf(request, principal, form.csrf_token):
        return _security_error_response(request, principal)

    settings: Settings = request.app.state.settings
    semantic_profile = _configured_semantic_profile_sha256(settings)
    semantic_threshold = await _effective_semantic_threshold(
        session,
        settings=settings,
        profile_sha256=semantic_profile,
    )
    expected_key = review_form_idempotency_key(
        settings,
        session_id=principal.session_id,
        action=_transition_action(target_state),
        parts=(str(review_task_id), str(form.expected_lock_version)),
    )
    if not form_key_matches(form.idempotency_key, expected_key):
        return _invalid_form_response(
            request,
            principal,
            status.HTTP_400_BAD_REQUEST,
        )
    try:
        await transition_review_task(
            session,
            review_task_id=review_task_id,
            target_state=target_state,
            changed_by_user_id=principal.user_id,
            expected_lock_version=form.expected_lock_version,
            idempotency_key=form.idempotency_key,
            semantic_profile_sha256=semantic_profile,
            semantic_severe_confidence_micros=semantic_threshold,
            semantic_enforcement_mode=settings.semantic_anatomy_mode,
        )
    except (ReviewInputError, ReviewNotFoundError, ReviewConflictError) as error:
        return _service_error_response(request, principal, error)
    return _review_redirect(request, review_task_id)


def _review_assets(
    settings: Settings,
    *,
    principal: AuthenticatedPrincipal,
    review_task_id: UUID,
    summary: ReviewSummary,
    masters: tuple[RankedMaster, ...],
    semantic_assessments: Mapping[UUID, SemanticReviewAssessment],
    semantic_feedback: Mapping[UUID, SemanticAnatomyFeedbackResult],
    inspected_asset_ids: frozenset[UUID],
    semantic_mode: str,
    severe_confidence_micros: int,
) -> ReviewAssetGroups:
    decisions = {decision.asset_id: decision for decision in summary.assets}
    if set(decisions) != {master.asset_id for master in masters}:
        raise RankingIntegrityError("review and ranking assets differ")
    is_open = summary.state == ReviewTaskState.OPEN

    def is_flagged(master: RankedMaster) -> bool:
        assessment = semantic_assessments.get(master.asset_id)
        return bool(
            assessment
            and assessment.is_high_confidence_severe(
                threshold_micros=severe_confidence_micros,
            )
        )

    views = tuple(
        ReviewAssetView(
            master=master,
            current=decisions[master.asset_id],
            semantic=semantic_assessments.get(master.asset_id),
            semantic_feedback=(
                semantic_feedback.get(semantic_assessments[master.asset_id].assessment_id)
                if master.asset_id in semantic_assessments
                else None
            ),
            semantic_flagged=is_flagged(master),
            ai_excluded=(
                semantic_mode == SemanticEnforcementMode.ENFORCE.value and is_flagged(master)
            ),
            inspected=master.asset_id in inspected_asset_ids,
            idempotency_key=(
                review_form_idempotency_key(
                    settings,
                    session_id=principal.session_id,
                    action="decision",
                    parts=(
                        str(review_task_id),
                        str(master.asset_id),
                        str(summary.lock_version),
                    ),
                )
                if is_open
                else None
            ),
            x_selection_idempotency_key=(
                review_form_idempotency_key(
                    settings,
                    session_id=principal.session_id,
                    action="x-selection",
                    parts=(
                        str(review_task_id),
                        str(master.asset_id),
                        str(not decisions[master.asset_id].selected_for_x).lower(),
                        str(summary.lock_version),
                    ),
                )
                if is_open and principal.role == AdminRole.OWNER
                else None
            ),
        )
        for master in masters
    )
    return ReviewAssetGroups(
        regular=tuple(view for view in views if not view.ai_excluded),
        ai_excluded=tuple(view for view in views if view.ai_excluded),
    )


def _configured_semantic_profile_sha256(settings: Settings) -> str | None:
    if not settings.semantic_anatomy_enabled:
        return None
    revision = settings.semantic_anatomy_model_revision
    if revision is None:
        raise RuntimeError("validated semantic anatomy settings are incomplete")
    return SemanticAssessmentProfile(
        model_name=settings.semantic_anatomy_model,
        model_revision=revision,
    ).profile_sha256


async def _effective_semantic_threshold(
    session: AsyncSession,
    *,
    settings: Settings,
    profile_sha256: str | None,
) -> int:
    configured = settings.semantic_anatomy_severe_confidence_micros
    if profile_sha256 is None:
        return configured
    return await load_effective_semantic_threshold_micros(
        session,
        profile_sha256=profile_sha256,
        configured_fallback_micros=configured,
    )


def _semantic_mode(settings: Settings) -> str:
    if not settings.semantic_anatomy_enabled:
        return "off"
    return settings.semantic_anatomy_mode.value


def _form_csrf_token(
    request: Request,
    principal: AuthenticatedPrincipal,
) -> str:
    settings: Settings = request.app.state.settings
    if not settings.auth_enabled:
        return _development_csrf_token(settings, principal)
    cookie_token = request.cookies.get(settings.auth_csrf_cookie_name)
    try:
        authentication_service(request).validate_csrf(
            principal,
            cookie_token=cookie_token,
            header_token=cookie_token,
        )
    except (CsrfValidationError, HTTPException):
        raise BrowserReviewSecurityError from None
    if cookie_token is None:
        raise BrowserReviewSecurityError
    return cookie_token


def _valid_form_csrf(
    request: Request,
    principal: AuthenticatedPrincipal,
    supplied_token: str,
) -> bool:
    settings: Settings = request.app.state.settings
    if not settings.auth_enabled:
        return hmac.compare_digest(
            supplied_token,
            _development_csrf_token(settings, principal),
        )
    try:
        authentication_service(request).validate_csrf(
            principal,
            cookie_token=request.cookies.get(settings.auth_csrf_cookie_name),
            header_token=supplied_token,
        )
    except (CsrfValidationError, HTTPException):
        return False
    return True


def _development_csrf_token(
    settings: Settings,
    principal: AuthenticatedPrincipal,
) -> str:
    return review_form_idempotency_key(
        settings,
        session_id=principal.session_id,
        action="csrf",
        parts=(),
    )


def _origin_error_response(
    request: Request,
    principal: AuthenticatedPrincipal,
) -> Response | None:
    try:
        require_same_origin(request)
    except HTTPException:
        return _security_error_response(request, principal)
    return None


def _review_redirect(request: Request, review_task_id: UUID) -> Response:
    return _secure_response(
        request,
        RedirectResponse(
            url=f"/dashboard/review-tasks/{review_task_id}",
            status_code=status.HTTP_303_SEE_OTHER,
        ),
    )


def _transition_action(target_state: ReviewTaskState) -> str:
    if target_state == ReviewTaskState.COMPLETED:
        return "complete"
    return "cancel"


def _object_store(request: Request) -> ObjectStore | None:
    store: ObjectStore | None = request.app.state.object_store
    return store


def _invalid_form_response(
    request: Request,
    principal: AuthenticatedPrincipal,
    status_code: int,
) -> Response:
    if status_code == status.HTTP_413_CONTENT_TOO_LARGE:
        message = "The submitted form was too large."
    elif status_code == status.HTTP_415_UNSUPPORTED_MEDIA_TYPE:
        message = "The submitted form type is not supported."
    else:
        message = "The submitted review form was invalid."
    return _review_action_error(
        request,
        principal,
        status_code=status_code,
        heading="Review change was not saved",
        message=message,
    )


def _security_error_response(
    request: Request,
    principal: AuthenticatedPrincipal,
) -> Response:
    return _review_action_error(
        request,
        principal,
        status_code=status.HTTP_403_FORBIDDEN,
        heading="Review change was not saved",
        message="The request could not be verified. Reload the page and try again.",
    )


def _service_error_response(
    request: Request,
    principal: AuthenticatedPrincipal,
    error: ReviewInputError | ReviewNotFoundError | ReviewConflictError,
) -> Response:
    if isinstance(error, ReviewInputError):
        return _review_action_error(
            request,
            principal,
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            heading="Review change was not saved",
            message="The submitted review change was invalid.",
        )
    if isinstance(error, ReviewNotFoundError):
        return _review_action_error(
            request,
            principal,
            status_code=status.HTTP_404_NOT_FOUND,
            heading="Review change was not saved",
            message="The requested review resource was not found.",
        )
    conflict_message = str(error)
    if conflict_message == "at most four images can be selected for one X post":
        heading = "Too many images for X"
        message = "An X post can use at most four images. Deselect an image and try again."
    elif conflict_message == "high-confidence severe anatomy requires an explicit owner override":
        heading = "Individual review required"
        message = (
            "AI-excluded images need individual owner review and an explicit override before "
            "acceptance."
        )
    else:
        heading = "Review changed"
        message = "This review changed before the form was submitted. Reload it and try again."
    return _review_action_error(
        request,
        principal,
        status_code=status.HTTP_409_CONFLICT,
        heading=heading,
        message=message,
    )


def _feedback_service_error_response(
    request: Request,
    principal: AuthenticatedPrincipal,
    error: (
        SemanticFeedbackAssessmentNotReadyError
        | SemanticFeedbackConflictError
        | SemanticFeedbackNotFoundError
        | SemanticFeedbackValidationError
    ),
) -> Response:
    if isinstance(error, SemanticFeedbackNotFoundError):
        status_code = status.HTTP_404_NOT_FOUND
        message = "The anatomy assessment no longer exists. Reload the review."
    elif isinstance(error, SemanticFeedbackValidationError):
        status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
        message = "That anatomy label is not valid for this assessment."
    elif isinstance(error, SemanticFeedbackAssessmentNotReadyError):
        status_code = status.HTTP_409_CONFLICT
        message = "The anatomy assessment is not ready for feedback yet."
    else:
        status_code = status.HTTP_409_CONFLICT
        message = "A different immutable label is already saved for this assessment."
    return _review_action_error(
        request,
        principal,
        status_code=status_code,
        heading="Anatomy feedback was not saved",
        message=message,
    )


def _review_action_error(
    request: Request,
    principal: AuthenticatedPrincipal,
    *,
    status_code: int,
    heading: str,
    message: str,
) -> Response:
    return _error_response(
        request,
        principal=principal,
        status_code=status_code,
        heading=heading,
        message=message,
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
