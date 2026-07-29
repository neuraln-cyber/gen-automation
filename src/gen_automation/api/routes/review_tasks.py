from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field

from gen_automation.api.security import (
    ReviewPrincipal,
    ReviewReader,
    Session,
)
from gen_automation.config import Settings
from gen_automation.domain.enums import ReviewDecisionValue, ReviewTaskState
from gen_automation.services.review import (
    ReviewConflictError,
    ReviewInputError,
    ReviewNotFoundError,
    ReviewSemanticGate,
    ReviewSummary,
    append_review_decision,
    create_review_task,
    get_review_summary,
    transition_review_task,
)
from gen_automation.services.semantic_anatomy import SemanticAssessmentProfile

router = APIRouter(prefix="/review-tasks", tags=["review tasks"])
IdempotencyKey = Annotated[
    str,
    Header(
        alias="Idempotency-Key",
        min_length=8,
        max_length=200,
        description="Stable key for this exact review command",
    ),
]


class _StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateReviewTaskRequest(_StrictRequest):
    scoring_run_id: UUID


class AppendReviewDecisionRequest(_StrictRequest):
    asset_id: UUID
    decision: ReviewDecisionValue
    expected_lock_version: int = Field(gt=0, le=2_147_483_647)
    reason_code: str | None = Field(default=None, max_length=100)
    note: str | None = Field(default=None, max_length=4_000)


class ReviewTransitionRequest(_StrictRequest):
    expected_lock_version: int = Field(gt=0, le=2_147_483_647)


class _AttributeResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class ReviewTaskResponse(_AttributeResponse):
    task_id: UUID
    release_version_id: UUID
    scoring_run_id: UUID
    desired_accepted_count: int
    ranked_asset_count: int
    state: ReviewTaskState
    lock_version: int
    replayed: bool


class ReviewDecisionResponse(_AttributeResponse):
    decision_id: UUID
    task_id: UUID
    asset_id: UUID
    revision: int
    decision: ReviewDecisionValue
    reason_code: str | None
    note: str | None
    decided_by_user_id: UUID
    supersedes_decision_id: UUID | None
    task_lock_version: int
    replayed: bool


class ReviewTransitionResponse(_AttributeResponse):
    task_id: UUID
    state: ReviewTaskState
    lock_version: int
    accepted_count: int
    replayed: bool


class CurrentAssetDecisionResponse(_AttributeResponse):
    asset_id: UUID
    rank: int
    decision_id: UUID | None
    revision: int | None
    decision: ReviewDecisionValue | None
    reason_code: str | None
    note: str | None
    decided_by_user_id: UUID | None
    decided_at: datetime | None
    selected_for_x: bool
    semantic_severe_override_attested: bool


class ReviewSemanticGateResponse(_AttributeResponse):
    enabled: bool
    ranked_asset_count: int
    terminal_count: int
    pending_count: int
    unavailable_count: int
    severe_count: int
    severe_override_count: int
    severe_blocked_count: int
    completion_ready: bool


class ReviewSummaryResponse(_AttributeResponse):
    task_id: UUID
    state: ReviewTaskState
    lock_version: int
    desired_accepted_count: int
    ranked_asset_count: int
    accepted_count: int
    rejected_count: int
    held_count: int
    undecided_count: int
    x_selected_count: int
    assets: tuple[CurrentAssetDecisionResponse, ...]
    semantic_gate: ReviewSemanticGateResponse


@router.post(
    "",
    response_model=ReviewTaskResponse,
    status_code=status.HTTP_201_CREATED,
)
async def post_review_task(
    command: CreateReviewTaskRequest,
    session: Session,
    idempotency_key: IdempotencyKey,
    response: Response,
    principal: ReviewPrincipal,
) -> ReviewTaskResponse:
    try:
        result = await create_review_task(
            session,
            scoring_run_id=command.scoring_run_id,
            created_by_user_id=principal.user_id,
            idempotency_key=idempotency_key,
        )
    except (ReviewInputError, ReviewNotFoundError, ReviewConflictError) as error:
        raise _http_error(error) from error
    _set_replay_response(response, replayed=result.replayed, created=True)
    return ReviewTaskResponse.model_validate(result)


@router.get("/{review_task_id}", response_model=ReviewSummaryResponse)
async def read_review_task(
    review_task_id: UUID,
    request: Request,
    session: Session,
    _principal: ReviewReader,
) -> ReviewSummaryResponse:
    semantic_profile, semantic_threshold = _semantic_gate_configuration(request)
    try:
        result = await get_review_summary(
            session,
            review_task_id=review_task_id,
            semantic_profile_sha256=semantic_profile,
            semantic_severe_confidence_micros=semantic_threshold,
        )
    except (ReviewInputError, ReviewNotFoundError, ReviewConflictError) as error:
        raise _http_error(error) from error
    return _summary_response(result)


@router.post(
    "/{review_task_id}/decisions",
    response_model=ReviewDecisionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def post_review_decision(
    review_task_id: UUID,
    command: AppendReviewDecisionRequest,
    request: Request,
    session: Session,
    idempotency_key: IdempotencyKey,
    response: Response,
    principal: ReviewPrincipal,
) -> ReviewDecisionResponse:
    semantic_profile, semantic_threshold = _semantic_gate_configuration(request)
    try:
        result = await append_review_decision(
            session,
            review_task_id=review_task_id,
            asset_id=command.asset_id,
            decision=command.decision,
            decided_by_user_id=principal.user_id,
            expected_lock_version=command.expected_lock_version,
            idempotency_key=idempotency_key,
            reason_code=command.reason_code,
            note=command.note,
            semantic_profile_sha256=semantic_profile,
            semantic_severe_confidence_micros=semantic_threshold,
        )
    except (ReviewInputError, ReviewNotFoundError, ReviewConflictError) as error:
        raise _http_error(error) from error
    _set_replay_response(response, replayed=result.replayed, created=True)
    return ReviewDecisionResponse.model_validate(result)


@router.post(
    "/{review_task_id}:complete",
    response_model=ReviewTransitionResponse,
)
async def complete_review_task(
    review_task_id: UUID,
    command: ReviewTransitionRequest,
    request: Request,
    session: Session,
    idempotency_key: IdempotencyKey,
    response: Response,
    principal: ReviewPrincipal,
) -> ReviewTransitionResponse:
    return await _transition(
        review_task_id=review_task_id,
        command=command,
        target_state=ReviewTaskState.COMPLETED,
        session=session,
        idempotency_key=idempotency_key,
        response=response,
        principal=principal,
        request=request,
    )


@router.post(
    "/{review_task_id}:cancel",
    response_model=ReviewTransitionResponse,
)
async def cancel_review_task(
    review_task_id: UUID,
    command: ReviewTransitionRequest,
    request: Request,
    session: Session,
    idempotency_key: IdempotencyKey,
    response: Response,
    principal: ReviewPrincipal,
) -> ReviewTransitionResponse:
    return await _transition(
        review_task_id=review_task_id,
        command=command,
        target_state=ReviewTaskState.CANCELLED,
        session=session,
        idempotency_key=idempotency_key,
        response=response,
        principal=principal,
        request=request,
    )


async def _transition(
    *,
    review_task_id: UUID,
    command: ReviewTransitionRequest,
    target_state: ReviewTaskState,
    session: Session,
    idempotency_key: str,
    response: Response,
    principal: ReviewPrincipal,
    request: Request,
) -> ReviewTransitionResponse:
    semantic_profile, semantic_threshold = _semantic_gate_configuration(request)
    try:
        result = await transition_review_task(
            session,
            review_task_id=review_task_id,
            target_state=target_state,
            changed_by_user_id=principal.user_id,
            expected_lock_version=command.expected_lock_version,
            idempotency_key=idempotency_key,
            semantic_profile_sha256=semantic_profile,
            semantic_severe_confidence_micros=semantic_threshold,
        )
    except (ReviewInputError, ReviewNotFoundError, ReviewConflictError) as error:
        raise _http_error(error) from error
    _set_replay_response(response, replayed=result.replayed, created=False)
    return ReviewTransitionResponse.model_validate(result)


def _summary_response(result: ReviewSummary) -> ReviewSummaryResponse:
    return ReviewSummaryResponse(
        task_id=result.task_id,
        state=result.state,
        lock_version=result.lock_version,
        desired_accepted_count=result.desired_accepted_count,
        ranked_asset_count=result.ranked_asset_count,
        accepted_count=result.accepted_count,
        rejected_count=result.rejected_count,
        held_count=result.held_count,
        undecided_count=result.undecided_count,
        x_selected_count=result.x_selected_count,
        assets=tuple(
            CurrentAssetDecisionResponse(
                asset_id=asset.asset_id,
                rank=asset.rank,
                decision_id=asset.decision_id,
                revision=asset.revision,
                decision=asset.decision,
                reason_code=asset.reason_code,
                note=asset.note,
                decided_by_user_id=asset.decided_by_user_id,
                decided_at=asset.decided_at,
                selected_for_x=asset.selected_for_x,
                semantic_severe_override_attested=(asset.semantic_severe_override_attested),
            )
            for asset in result.assets
        ),
        semantic_gate=_semantic_gate_response(result.semantic_gate),
    )


def _semantic_gate_response(result: ReviewSemanticGate) -> ReviewSemanticGateResponse:
    return ReviewSemanticGateResponse(
        enabled=result.enabled,
        ranked_asset_count=result.ranked_asset_count,
        terminal_count=result.terminal_count,
        pending_count=result.pending_count,
        unavailable_count=result.unavailable_count,
        severe_count=result.severe_count,
        severe_override_count=result.severe_override_count,
        severe_blocked_count=result.severe_blocked_count,
        completion_ready=result.completion_ready,
    )


def _semantic_gate_configuration(request: Request) -> tuple[str | None, int]:
    settings: Settings = request.app.state.settings
    if not settings.semantic_anatomy_enabled:
        return None, settings.semantic_anatomy_severe_confidence_micros
    revision = settings.semantic_anatomy_model_revision
    if revision is None:
        raise RuntimeError("validated semantic anatomy settings are incomplete")
    return (
        SemanticAssessmentProfile(
            model_name=settings.semantic_anatomy_model,
            model_revision=revision,
        ).profile_sha256,
        settings.semantic_anatomy_severe_confidence_micros,
    )


def _set_replay_response(
    response: Response,
    *,
    replayed: bool,
    created: bool,
) -> None:
    response.headers["Idempotency-Replayed"] = str(replayed).lower()
    if created and replayed:
        response.status_code = status.HTTP_200_OK


def _http_error(
    error: ReviewInputError | ReviewNotFoundError | ReviewConflictError,
) -> HTTPException:
    if isinstance(error, ReviewInputError):
        return HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="review request is invalid",
        )
    if isinstance(error, ReviewNotFoundError):
        return HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="review resource was not found",
        )
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail="review request conflicts with current state",
    )
