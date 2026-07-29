import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from gen_automation.db.models import (
    AdminUser,
    Asset,
    AssetLineage,
    AuditEvent,
    DerivativeJob,
    DerivativeOutput,
    DerivativeRecipe,
    IdempotencyRecord,
    OutboxEvent,
    Release,
    ReleaseSelection,
    ReleaseVersion,
    ReviewTask,
    ReviewXSelection,
)
from gen_automation.domain.canonical import canonical_json_bytes, canonical_sha256
from gen_automation.domain.deliverability import (
    DeliverabilityError,
    patreon_full_output_byte_budget,
)
from gen_automation.domain.enums import (
    AdminRole,
    AssetKind,
    AssetState,
    DerivativeJobState,
    OutboxStatus,
    ReleasePhase,
    ReviewTaskState,
)
from gen_automation.domain.ids import uuid7
from gen_automation.services.watermarks import is_registered_watermark

_SHA256 = re.compile(r"[0-9a-f]{64}")
_TARGET = re.compile(r"[a-z][a-z0-9_-]{0,49}")
_CODE = re.compile(r"[a-z][a-z0-9_.-]{0,99}")
_MAX_CONFIGURATION_BYTES = 64 * 1024
_MAX_ERROR_DETAIL = 4_000
_FULL_TARGET = "full"
_X_TARGET = "x_teaser"
_SUPPORTED_TARGETS = frozenset({_FULL_TARGET, _X_TARGET})
_TERMINAL_STATES = frozenset(
    {
        DerivativeJobState.SUCCEEDED,
        DerivativeJobState.FAILED,
        DerivativeJobState.CANCELLED,
    }
)


class DerivativePipelineError(Exception):
    """Base error for durable derivative planning and job state."""


class DerivativePipelineNotFoundError(DerivativePipelineError):
    pass


class DerivativePipelineConflictError(DerivativePipelineError):
    pass


class DerivativePipelineInputError(DerivativePipelineError, ValueError):
    pass


class DerivativePipelineLeaseError(DerivativePipelineConflictError):
    pass


@dataclass(frozen=True, slots=True)
class DerivativePlanResult:
    review_task_id: UUID
    recipe_id: UUID
    release_version_id: UUID
    job_ids: tuple[UUID, ...]
    jobs_created: int
    total_jobs: int
    replayed: bool


@dataclass(frozen=True, slots=True)
class ClaimedDerivativeJob:
    job_id: UUID
    release_selection_id: UUID
    derivative_recipe_id: UUID
    request_payload: dict[str, Any]
    request_sha256: str
    attempt_count: int
    lock_version: int
    lease_expires_at: datetime


@dataclass(frozen=True, slots=True)
class DerivativeJobResult:
    job_id: UUID
    state: DerivativeJobState
    attempt_count: int
    lock_version: int
    retry_at: datetime | None
    completed_at: datetime | None


@dataclass(frozen=True, slots=True)
class DerivativeOutputResult:
    output_id: UUID
    job_id: UUID
    target: str
    asset_id: UUID
    lineage_id: UUID
    replayed: bool


async def create_derivative_recipe_and_plan(
    session: AsyncSession,
    *,
    review_task_id: UUID,
    configuration: Mapping[str, Any],
    recipe_version: int,
    renderer_version: str,
    pillow_version: str,
    created_by_user_id: UUID,
    approved_by_user_id: UUID,
    idempotency_key: str,
    output_targets: Sequence[str] = ("full", "x_teaser"),
    watermark_asset_id: UUID | None = None,
    max_attempts: int = 3,
    priority: int = 100,
    now: datetime | None = None,
) -> DerivativePlanResult:
    """Approve one immutable recipe and plan one job per frozen selection."""

    normalized_configuration = _normalize_configuration(configuration)
    normalized_targets = _normalize_targets(output_targets)
    if _FULL_TARGET not in normalized_targets:
        raise DerivativePipelineInputError(
            "the clean full target is required for every accepted image"
        )
    normalized_renderer = _bounded_text(renderer_version, "renderer version", 100)
    normalized_pillow = _bounded_text(pillow_version, "Pillow version", 50)
    normalized_key = _bounded_text(idempotency_key, "idempotency key", 200)
    normalized_recipe_version = _bounded_int(
        recipe_version,
        "recipe version",
        minimum=1,
        maximum=1_000_000,
    )
    normalized_max_attempts = _bounded_int(
        max_attempts,
        "maximum attempts",
        minimum=1,
        maximum=100,
    )
    normalized_priority = _bounded_int(
        priority,
        "priority",
        minimum=-1_000_000,
        maximum=1_000_000,
    )
    planned_at = _as_utc(now or datetime.now(UTC))
    await _require_active_planner(session, created_by_user_id)
    await _require_active_planner(session, approved_by_user_id)

    for attempt in range(2):
        try:
            return await _create_plan_once(
                session,
                review_task_id=review_task_id,
                configuration=normalized_configuration,
                recipe_version=normalized_recipe_version,
                renderer_version=normalized_renderer,
                pillow_version=normalized_pillow,
                created_by_user_id=created_by_user_id,
                approved_by_user_id=approved_by_user_id,
                idempotency_key=normalized_key,
                output_targets=normalized_targets,
                watermark_asset_id=watermark_asset_id,
                max_attempts=normalized_max_attempts,
                priority=normalized_priority,
                planned_at=planned_at,
            )
        except IntegrityError as error:
            await session.rollback()
            if attempt:
                raise DerivativePipelineConflictError(
                    "derivative plan was created concurrently"
                ) from error
    raise AssertionError("unreachable derivative plan retry")


async def _create_plan_once(
    session: AsyncSession,
    *,
    review_task_id: UUID,
    configuration: dict[str, Any],
    recipe_version: int,
    renderer_version: str,
    pillow_version: str,
    created_by_user_id: UUID,
    approved_by_user_id: UUID,
    idempotency_key: str,
    output_targets: tuple[str, ...],
    watermark_asset_id: UUID | None,
    max_attempts: int,
    priority: int,
    planned_at: datetime,
) -> DerivativePlanResult:
    task = await session.scalar(
        select(ReviewTask).where(ReviewTask.id == review_task_id).with_for_update()
    )
    if task is None:
        raise DerivativePipelineNotFoundError("review task was not found")
    if task.state != ReviewTaskState.COMPLETED:
        raise DerivativePipelineConflictError(
            "derivative planning requires a completed review task"
        )
    selections = await _load_and_validate_selections(session, task)
    try:
        full_output_byte_budget = patreon_full_output_byte_budget(len(selections))
    except DeliverabilityError as error:
        raise DerivativePipelineConflictError(str(error)) from None
    x_selected_asset_ids = await _load_x_selected_asset_ids(
        session,
        task=task,
        selections=selections,
    )
    if x_selected_asset_ids and _X_TARGET not in output_targets:
        raise DerivativePipelineConflictError(
            "the approved recipe omits the selected X teaser target"
        )
    if x_selected_asset_ids and watermark_asset_id is None:
        raise DerivativePipelineConflictError(
            "selected X images require an approved watermark asset"
        )
    if x_selected_asset_ids and configuration.get("watermark") is None:
        raise DerivativePipelineConflictError(
            "selected X images require a canonical watermark recipe"
        )
    watermark = await _load_watermark_snapshot(
        session,
        release_version_id=task.release_version_id,
        watermark_asset_id=watermark_asset_id,
    )

    config_sha256 = canonical_sha256(configuration)
    selected_selections = [
        selection for selection in selections if selection.asset_id in x_selected_asset_ids
    ]
    clean_only_selections = [
        selection for selection in selections if selection.asset_id not in x_selected_asset_ids
    ]
    target_groups: list[tuple[tuple[str, ...], list[ReleaseSelection]]] = []
    if selected_selections:
        target_groups.append((output_targets, selected_selections))
    if clean_only_selections:
        target_groups.append(((_FULL_TARGET,), clean_only_selections))
    recipe_plans: list[
        tuple[
            tuple[str, ...],
            list[ReleaseSelection],
            dict[str, Any],
            str,
        ]
    ] = []
    for group_targets, group_selections in target_groups:
        identity = {
            "schema": "derivative-recipe-identity/v1",
            "release_version_id": str(task.release_version_id),
            "recipe_version": recipe_version,
            "config_sha256": config_sha256,
            "output_targets": list(group_targets),
            "renderer_version": renderer_version,
            "pillow_version": pillow_version,
            "watermark": watermark,
        }
        recipe_plans.append(
            (
                group_targets,
                group_selections,
                identity,
                canonical_sha256(identity),
            )
        )
    scope = f"review-task:{task.id}:derivative-plan"
    request_sha256 = canonical_sha256(
        {
            "schema": "derivative-plan-request/v1",
            "review_task_id": str(task.id),
            "recipes": [
                {
                    "logical_key": logical_key,
                    "selection_ids": [str(selection.id) for selection in group_selections],
                }
                for _, group_selections, _, logical_key in recipe_plans
            ],
            "created_by_user_id": str(created_by_user_id),
            "approved_by_user_id": str(approved_by_user_id),
            "max_attempts": max_attempts,
            "priority": priority,
            "x_selected_asset_ids": sorted(str(asset_id) for asset_id in x_selected_asset_ids),
        }
    )
    replay = await _plan_replay(
        session,
        scope=scope,
        idempotency_key=idempotency_key,
        request_sha256=request_sha256,
    )
    if replay is not None:
        return replay

    release_row = (
        await session.execute(
            select(Release, ReleaseVersion)
            .join(
                ReleaseVersion,
                ReleaseVersion.release_id == Release.id,
            )
            .where(ReleaseVersion.id == task.release_version_id)
            .with_for_update()
        )
    ).one_or_none()
    if release_row is None:
        raise DerivativePipelineConflictError("derivative plan release version is unavailable")
    release, release_version = release_row
    if (
        release.current_version_no != release_version.version_no
        or release.phase != ReleasePhase.APPROVED
    ):
        raise DerivativePipelineConflictError(
            "derivative plan is stale or the release phase does not allow rendering"
        )
    promoted_release_id = await session.scalar(
        update(Release)
        .where(
            Release.id == release.id,
            Release.current_version_no == release_version.version_no,
            Release.phase == ReleasePhase.APPROVED,
            Release.lock_version == release.lock_version,
        )
        .values(
            phase=ReleasePhase.RENDERING,
            lock_version=Release.lock_version + 1,
        )
        .returning(Release.id)
    )
    if promoted_release_id is None:
        raise DerivativePipelineConflictError(
            "derivative plan release phase compare-and-swap failed"
        )
    session.add(
        _audit(
            actor=f"admin:{approved_by_user_id}",
            action="release.derivative_rendering_started",
            resource_type="release",
            resource_id=release.id,
            correlation_id=idempotency_key,
            detail={
                "review_task_id": str(task.id),
                "release_version_id": str(task.release_version_id),
                "phase": ReleasePhase.RENDERING.value,
            },
            occurred_at=planned_at,
        )
    )
    session.add(
        _outbox(
            topic="release.derivative_rendering_started",
            dedupe_key=f"release.derivative_rendering_started:{task.id}",
            correlation_id=idempotency_key,
            aggregate_type="release",
            aggregate_id=release.id,
            payload={
                "release_id": str(release.id),
                "release_version_id": str(task.release_version_id),
                "review_task_id": str(task.id),
            },
            occurred_at=planned_at,
        )
    )

    recipes: list[DerivativeRecipe] = []
    job_ids: list[UUID] = []
    jobs_created = 0
    recipes_created = 0
    for group_targets, group_selections, recipe_identity, recipe_logical_key in recipe_plans:
        recipe = await session.scalar(
            select(DerivativeRecipe).where(DerivativeRecipe.logical_key == recipe_logical_key)
        )
        if recipe is None:
            recipe = DerivativeRecipe(
                id=uuid7(),
                release_version_id=task.release_version_id,
                logical_key=recipe_logical_key,
                recipe_version=recipe_version,
                configuration=configuration,
                config_sha256=config_sha256,
                output_targets=list(group_targets),
                expected_output_count=len(group_targets),
                renderer_version=renderer_version,
                pillow_version=pillow_version,
                watermark_asset_id=watermark_asset_id,
                watermark_storage_backend=_optional_string(
                    watermark,
                    "storage_backend",
                ),
                watermark_storage_bucket=_optional_string(
                    watermark,
                    "storage_bucket",
                ),
                watermark_object_key=_optional_string(watermark, "object_key"),
                watermark_object_version_id=_optional_string(
                    watermark,
                    "object_version_id",
                ),
                watermark_sha256=_optional_string(watermark, "sha256"),
                watermark_content_type=_optional_string(watermark, "content_type"),
                watermark_image_format=_optional_string(watermark, "image_format"),
                watermark_width=_optional_int(watermark, "width"),
                watermark_height=_optional_int(watermark, "height"),
                watermark_byte_size=_optional_int(watermark, "byte_size"),
                created_by_user_id=created_by_user_id,
                created_at=planned_at,
                approved_by_user_id=approved_by_user_id,
                approved_at=planned_at,
            )
            session.add(recipe)
            await session.flush()
            recipes_created += 1
            session.add(
                _audit(
                    actor=f"admin:{approved_by_user_id}",
                    action="derivative.recipe_approved",
                    resource_type="derivative_recipe",
                    resource_id=recipe.id,
                    correlation_id=idempotency_key,
                    detail={
                        "release_version_id": str(task.release_version_id),
                        "logical_key": recipe.logical_key,
                        "config_sha256": recipe.config_sha256,
                        "recipe_version": recipe.recipe_version,
                        "output_targets": list(group_targets),
                        "created_by_user_id": str(created_by_user_id),
                        "approved_by_user_id": str(approved_by_user_id),
                        "watermark_asset_id": (
                            str(watermark_asset_id) if watermark_asset_id is not None else None
                        ),
                    },
                    occurred_at=planned_at,
                )
            )
        else:
            _validate_existing_recipe(
                recipe,
                release_version_id=task.release_version_id,
                recipe_identity=recipe_identity,
                configuration=configuration,
                output_targets=group_targets,
            )
        recipes.append(recipe)

        existing_jobs = list(
            (
                await session.scalars(
                    select(DerivativeJob)
                    .where(DerivativeJob.derivative_recipe_id == recipe.id)
                    .order_by(DerivativeJob.logical_key)
                )
            ).all()
        )
        jobs_by_key = {job.logical_key: job for job in existing_jobs}
        for selection in group_selections:
            logical_key = canonical_sha256(
                {
                    "schema": "derivative-job-identity/v1",
                    "release_selection_id": str(selection.id),
                    "recipe_logical_key": recipe.logical_key,
                    "output_targets": list(group_targets),
                }
            )
            request_payload = _job_request_payload(
                task=task,
                selection=selection,
                recipe=recipe,
                output_targets=group_targets,
                full_output_byte_budget=full_output_byte_budget,
            )
            job_request_sha256 = canonical_sha256(request_payload)
            existing = jobs_by_key.get(logical_key)
            if existing is not None:
                if (
                    existing.release_selection_id != selection.id
                    or existing.release_version_id != task.release_version_id
                    or existing.request_sha256 != job_request_sha256
                    or existing.request_payload != request_payload
                    or existing.expected_output_count != len(group_targets)
                    or existing.max_attempts != max_attempts
                    or existing.priority != priority
                ):
                    raise DerivativePipelineConflictError(
                        "existing derivative job conflicts with the frozen plan"
                    )
                job_ids.append(existing.id)
                continue

            job = DerivativeJob(
                id=uuid7(),
                release_selection_id=selection.id,
                derivative_recipe_id=recipe.id,
                release_version_id=task.release_version_id,
                logical_key=logical_key,
                request_payload=request_payload,
                request_sha256=job_request_sha256,
                expected_output_count=len(group_targets),
                state=DerivativeJobState.REQUESTED,
                priority=priority,
                attempt_count=0,
                max_attempts=max_attempts,
                lock_version=1,
                available_at=planned_at,
                requested_at=planned_at,
            )
            session.add(job)
            job_ids.append(job.id)
            jobs_created += 1
            session.add(
                _outbox(
                    topic="derivative.job.requested",
                    dedupe_key=f"derivative.job.requested:{job.id}",
                    correlation_id=idempotency_key,
                    aggregate_type="derivative_job",
                    aggregate_id=job.id,
                    payload={
                        "job_id": str(job.id),
                        "release_selection_id": str(selection.id),
                        "derivative_recipe_id": str(recipe.id),
                        "request_sha256": job_request_sha256,
                    },
                    occurred_at=planned_at,
                )
            )

    await session.flush()
    total_jobs = int(
        await session.scalar(
            select(func.count()).select_from(DerivativeJob).where(DerivativeJob.id.in_(job_ids))
        )
        or 0
    )
    if total_jobs != len(selections):
        raise DerivativePipelineConflictError(
            "derivative job count conflicts with the frozen selections"
        )
    ordered_job_ids = tuple(
        (
            await session.scalars(
                select(DerivativeJob.id)
                .join(
                    ReleaseSelection,
                    ReleaseSelection.id == DerivativeJob.release_selection_id,
                )
                .where(DerivativeJob.id.in_(job_ids))
                .order_by(ReleaseSelection.display_order)
            )
        ).all()
    )
    result = DerivativePlanResult(
        review_task_id=task.id,
        recipe_id=recipes[0].id,
        release_version_id=task.release_version_id,
        job_ids=ordered_job_ids,
        jobs_created=jobs_created,
        total_jobs=total_jobs,
        replayed=False,
    )
    session.add(
        _audit(
            actor=f"admin:{approved_by_user_id}",
            action="derivative.plan_created",
            resource_type="review_task",
            resource_id=task.id,
            correlation_id=idempotency_key,
            detail={
                "recipe_id": str(recipes[0].id),
                "recipe_ids": [str(recipe.id) for recipe in recipes],
                "recipes_created": recipes_created,
                "jobs_created": jobs_created,
                "total_jobs": total_jobs,
                "job_ids": [str(job_id) for job_id in ordered_job_ids],
            },
            occurred_at=planned_at,
        )
    )
    session.add(
        IdempotencyRecord(
            scope=scope,
            idempotency_key=idempotency_key,
            request_sha256=request_sha256,
            response_status=201,
            response_body=_plan_response_body(result),
            created_at=planned_at,
            expires_at=planned_at + timedelta(days=30),
        )
    )
    await session.commit()
    return result


async def claim_derivative_jobs(
    session: AsyncSession,
    *,
    worker_id: str,
    limit: int = 10,
    lease_seconds: int = 900,
    now: datetime | None = None,
) -> list[ClaimedDerivativeJob]:
    normalized_worker = _bounded_text(worker_id, "worker id", 200)
    normalized_limit = _bounded_int(limit, "claim limit", minimum=1, maximum=100)
    normalized_lease = _bounded_int(
        lease_seconds,
        "lease seconds",
        minimum=30,
        maximum=3_600,
    )
    claimed_at = _as_utc(now or datetime.now(UTC))
    claimable = or_(
        (
            (DerivativeJob.state == DerivativeJobState.REQUESTED)
            & (DerivativeJob.available_at <= claimed_at)
        ),
        (
            (DerivativeJob.state == DerivativeJobState.RETRY_WAIT)
            & (DerivativeJob.retry_at.is_not(None))
            & (DerivativeJob.retry_at <= claimed_at)
        ),
        (
            DerivativeJob.state.in_((DerivativeJobState.CLAIMED, DerivativeJobState.PROCESSING))
            & (DerivativeJob.lease_expires_at.is_not(None))
            & (DerivativeJob.lease_expires_at <= claimed_at)
        ),
    )
    candidate_ids = list(
        (
            await session.scalars(
                select(DerivativeJob.id)
                .where(
                    claimable,
                    DerivativeJob.attempt_count < DerivativeJob.max_attempts,
                )
                .order_by(
                    DerivativeJob.priority,
                    DerivativeJob.available_at,
                    DerivativeJob.id,
                )
                .limit(normalized_limit)
                .with_for_update(skip_locked=True)
            )
        ).all()
    )
    lease_expires_at = claimed_at + timedelta(seconds=normalized_lease)
    claimed: list[ClaimedDerivativeJob] = []
    for job_id in candidate_ids:
        row = (
            await session.execute(
                update(DerivativeJob)
                .where(
                    DerivativeJob.id == job_id,
                    claimable,
                    DerivativeJob.attempt_count < DerivativeJob.max_attempts,
                )
                .values(
                    state=DerivativeJobState.CLAIMED,
                    attempt_count=DerivativeJob.attempt_count + 1,
                    lock_version=DerivativeJob.lock_version + 1,
                    retry_at=None,
                    lease_owner=normalized_worker,
                    lease_expires_at=lease_expires_at,
                    claimed_at=claimed_at,
                    processing_started_at=None,
                    last_error_code=None,
                    last_error_detail=None,
                )
                .returning(DerivativeJob)
            )
        ).scalar_one_or_none()
        if row is None:
            continue
        claimed.append(
            ClaimedDerivativeJob(
                job_id=row.id,
                release_selection_id=row.release_selection_id,
                derivative_recipe_id=row.derivative_recipe_id,
                request_payload=dict(row.request_payload),
                request_sha256=row.request_sha256,
                attempt_count=row.attempt_count,
                lock_version=row.lock_version,
                lease_expires_at=lease_expires_at,
            )
        )
        session.add(
            _audit(
                actor=normalized_worker,
                action="derivative.job_claimed",
                resource_type="derivative_job",
                resource_id=row.id,
                correlation_id=str(row.id),
                detail={
                    "attempt_count": row.attempt_count,
                    "lease_expires_at": lease_expires_at.isoformat(),
                },
                occurred_at=claimed_at,
            )
        )
        session.add(
            _outbox(
                topic="derivative.job.claimed",
                dedupe_key=f"derivative.job.claimed:{row.id}:{row.attempt_count}",
                correlation_id=str(row.id),
                aggregate_type="derivative_job",
                aggregate_id=row.id,
                payload={
                    "job_id": str(row.id),
                    "attempt_count": row.attempt_count,
                    "worker_id": normalized_worker,
                },
                occurred_at=claimed_at,
            )
        )
    await session.commit()
    return claimed


async def start_derivative_job(
    session: AsyncSession,
    *,
    job_id: UUID,
    worker_id: str,
    expected_lock_version: int,
    now: datetime | None = None,
) -> DerivativeJobResult:
    started_at = _as_utc(now or datetime.now(UTC))
    worker = _bounded_text(worker_id, "worker id", 200)
    expected = _positive_lock_version(expected_lock_version)
    job = await _load_job_locked(session, job_id)
    _require_active_lease(
        job,
        worker_id=worker,
        expected_lock_version=expected,
        now=started_at,
        allowed_states=(DerivativeJobState.CLAIMED,),
    )
    job.state = DerivativeJobState.PROCESSING
    job.processing_started_at = started_at
    job.lock_version += 1
    _record_job_transition(
        session,
        job=job,
        actor=worker,
        action="derivative.job_processing",
        occurred_at=started_at,
        detail={"attempt_count": job.attempt_count},
    )
    await session.commit()
    return _job_result(job)


async def retry_derivative_job(
    session: AsyncSession,
    *,
    job_id: UUID,
    worker_id: str,
    expected_lock_version: int,
    retry_at: datetime,
    error_code: str,
    error_detail: str | None = None,
    now: datetime | None = None,
) -> DerivativeJobResult:
    deferred_at = _as_utc(now or datetime.now(UTC))
    normalized_retry = _as_utc(retry_at)
    if normalized_retry <= deferred_at:
        raise DerivativePipelineInputError("retry time must be in the future")
    worker = _bounded_text(worker_id, "worker id", 200)
    expected = _positive_lock_version(expected_lock_version)
    code = _error_code(error_code)
    detail = _error_detail(error_detail)
    job = await _load_job_locked(session, job_id)
    _require_active_lease(
        job,
        worker_id=worker,
        expected_lock_version=expected,
        now=deferred_at,
        allowed_states=(
            DerivativeJobState.CLAIMED,
            DerivativeJobState.PROCESSING,
        ),
    )
    if job.attempt_count >= job.max_attempts:
        raise DerivativePipelineConflictError("derivative job has exhausted its retry attempts")
    job.state = DerivativeJobState.RETRY_WAIT
    job.retry_at = normalized_retry
    job.lease_owner = None
    job.lease_expires_at = None
    job.last_error_code = code
    job.last_error_detail = detail
    job.lock_version += 1
    _record_job_transition(
        session,
        job=job,
        actor=worker,
        action="derivative.job_retry_scheduled",
        occurred_at=deferred_at,
        detail={
            "attempt_count": job.attempt_count,
            "retry_at": normalized_retry.isoformat(),
            "error_code": code,
        },
    )
    await session.commit()
    return _job_result(job)


async def record_derivative_output(
    session: AsyncSession,
    *,
    job_id: UUID,
    target: str,
    asset_id: UUID,
    worker_id: str,
    expected_lock_version: int,
    now: datetime | None = None,
) -> DerivativeOutputResult:
    recorded_at = _as_utc(now or datetime.now(UTC))
    normalized_target = _normalize_target(target)
    worker = _bounded_text(worker_id, "worker id", 200)
    expected = _positive_lock_version(expected_lock_version)
    existing = await session.scalar(
        select(DerivativeOutput).where(
            DerivativeOutput.derivative_job_id == job_id,
            DerivativeOutput.target == normalized_target,
        )
    )
    if existing is not None:
        if existing.asset_id != asset_id:
            raise DerivativePipelineConflictError(
                "derivative target already references another asset"
            )
        return _output_result(existing, replayed=True)

    row = (
        await session.execute(
            select(DerivativeJob, ReleaseSelection, DerivativeRecipe)
            .join(
                ReleaseSelection,
                ReleaseSelection.id == DerivativeJob.release_selection_id,
            )
            .join(
                DerivativeRecipe,
                DerivativeRecipe.id == DerivativeJob.derivative_recipe_id,
            )
            .where(DerivativeJob.id == job_id)
            .with_for_update()
        )
    ).one_or_none()
    if row is None:
        raise DerivativePipelineNotFoundError("derivative job was not found")
    job, selection, recipe = row
    _require_active_lease(
        job,
        worker_id=worker,
        expected_lock_version=expected,
        now=recorded_at,
        allowed_states=(DerivativeJobState.PROCESSING,),
    )
    if normalized_target not in _stored_targets(recipe):
        raise DerivativePipelineConflictError(
            "derivative target is not part of the approved recipe"
        )
    asset = await session.scalar(select(Asset).where(Asset.id == asset_id).with_for_update())
    if asset is None:
        raise DerivativePipelineNotFoundError("derivative asset was not found")
    release_version = await session.get(ReleaseVersion, job.release_version_id)
    if release_version is None:
        raise DerivativePipelineConflictError("derivative job release version is unavailable")
    _validate_available_asset(
        asset,
        expected_kind=AssetKind.DERIVATIVE,
        expected_release_id=release_version.release_id,
        label="derivative asset",
    )
    if asset.id == selection.asset_id:
        raise DerivativePipelineConflictError("derivative output cannot reuse its raw source asset")

    lineage = await session.scalar(
        select(AssetLineage).where(
            AssetLineage.parent_asset_id == selection.asset_id,
            AssetLineage.child_asset_id == asset.id,
            AssetLineage.relation == "derivative",
            AssetLineage.recipe_version == recipe.config_sha256,
        )
    )
    if lineage is None:
        lineage = AssetLineage(
            id=uuid7(),
            parent_asset_id=selection.asset_id,
            child_asset_id=asset.id,
            relation="derivative",
            recipe_version=recipe.config_sha256,
            created_at=recorded_at,
        )
        session.add(lineage)
        await session.flush()

    output = DerivativeOutput(
        id=uuid7(),
        derivative_job_id=job.id,
        release_selection_id=selection.id,
        derivative_recipe_id=recipe.id,
        target=normalized_target,
        asset_id=asset.id,
        source_asset_id=selection.asset_id,
        asset_lineage_id=lineage.id,
        asset_storage_backend=asset.storage_backend,
        asset_storage_bucket=asset.storage_bucket,
        asset_object_key=_required_asset_text(asset.object_key, "object key"),
        asset_object_version_id=_required_asset_text(
            asset.object_version_id,
            "object version",
        ),
        asset_sha256=_required_asset_text(asset.sha256, "SHA-256"),
        asset_content_type=_required_asset_text(asset.content_type, "content type"),
        asset_image_format=_required_asset_text(asset.image_format, "image format"),
        asset_width=_required_asset_int(asset.width, "width"),
        asset_height=_required_asset_int(asset.height, "height"),
        asset_byte_size=_required_asset_int(asset.byte_size, "byte size"),
        lineage_relation=lineage.relation,
        lineage_recipe_version=lineage.recipe_version,
        recorded_by=worker,
        recorded_at=recorded_at,
    )
    session.add(output)
    session.add(
        _audit(
            actor=worker,
            action="derivative.output_recorded",
            resource_type="derivative_output",
            resource_id=output.id,
            correlation_id=str(job.id),
            detail={
                "job_id": str(job.id),
                "target": normalized_target,
                "asset_id": str(asset.id),
                "asset_sha256": asset.sha256,
                "lineage_id": str(lineage.id),
            },
            occurred_at=recorded_at,
        )
    )
    session.add(
        _outbox(
            topic="derivative.output.recorded",
            dedupe_key=f"derivative.output.recorded:{job.id}:{normalized_target}",
            correlation_id=str(job.id),
            aggregate_type="derivative_output",
            aggregate_id=output.id,
            payload={
                "job_id": str(job.id),
                "target": normalized_target,
                "asset_id": str(asset.id),
                "asset_sha256": asset.sha256,
            },
            occurred_at=recorded_at,
        )
    )
    try:
        await session.commit()
    except IntegrityError as error:
        await session.rollback()
        existing = await session.scalar(
            select(DerivativeOutput).where(
                DerivativeOutput.derivative_job_id == job_id,
                DerivativeOutput.target == normalized_target,
            )
        )
        if existing is not None and existing.asset_id == asset_id:
            return _output_result(existing, replayed=True)
        raise DerivativePipelineConflictError(
            "derivative output was recorded concurrently"
        ) from error
    return _output_result(output, replayed=False)


async def succeed_derivative_job(
    session: AsyncSession,
    *,
    job_id: UUID,
    worker_id: str,
    expected_lock_version: int,
    now: datetime | None = None,
) -> DerivativeJobResult:
    completed_at = _as_utc(now or datetime.now(UTC))
    worker = _bounded_text(worker_id, "worker id", 200)
    expected = _positive_lock_version(expected_lock_version)
    row = (
        await session.execute(
            select(DerivativeJob, DerivativeRecipe)
            .join(
                DerivativeRecipe,
                DerivativeRecipe.id == DerivativeJob.derivative_recipe_id,
            )
            .where(DerivativeJob.id == job_id)
            .with_for_update()
        )
    ).one_or_none()
    if row is None:
        raise DerivativePipelineNotFoundError("derivative job was not found")
    job, recipe = row
    _require_active_lease(
        job,
        worker_id=worker,
        expected_lock_version=expected,
        now=completed_at,
        allowed_states=(DerivativeJobState.PROCESSING,),
    )
    targets = tuple(
        (
            await session.scalars(
                select(DerivativeOutput.target)
                .where(DerivativeOutput.derivative_job_id == job.id)
                .order_by(DerivativeOutput.target)
            )
        ).all()
    )
    expected_targets = _job_targets(
        job.request_payload,
        recipe_targets=_stored_targets(recipe),
    )
    if targets != expected_targets:
        raise DerivativePipelineConflictError(
            "derivative outputs do not satisfy the frozen job targets"
        )
    job.state = DerivativeJobState.SUCCEEDED
    job.lease_owner = None
    job.lease_expires_at = None
    job.completed_at = completed_at
    job.last_error_code = None
    job.last_error_detail = None
    job.lock_version += 1
    _record_job_transition(
        session,
        job=job,
        actor=worker,
        action="derivative.job_succeeded",
        occurred_at=completed_at,
        detail={"output_targets": list(targets)},
    )
    try:
        await session.flush()
    except IntegrityError as error:
        await session.rollback()
        raise DerivativePipelineConflictError(
            "derivative job completion snapshot is invalid"
        ) from error
    remaining_jobs = int(
        await session.scalar(
            select(func.count())
            .select_from(DerivativeJob)
            .where(
                DerivativeJob.release_version_id == job.release_version_id,
                DerivativeJob.state != DerivativeJobState.SUCCEEDED,
            )
        )
        or 0
    )
    if remaining_jobs == 0:
        release = await session.scalar(
            select(Release)
            .join(
                ReleaseVersion,
                ReleaseVersion.release_id == Release.id,
            )
            .where(ReleaseVersion.id == job.release_version_id)
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        if release is None or release.phase != ReleasePhase.READY_TO_PUBLISH:
            await session.rollback()
            raise DerivativePipelineConflictError("release readiness transition did not complete")
        session.add(
            _audit(
                actor=worker,
                action="release.ready_to_publish",
                resource_type="release",
                resource_id=release.id,
                correlation_id=str(job.id),
                detail={
                    "release_version_id": str(job.release_version_id),
                    "phase": ReleasePhase.READY_TO_PUBLISH.value,
                },
                occurred_at=completed_at,
            )
        )
        session.add(
            _outbox(
                topic="release.ready_to_publish",
                dedupe_key=f"release.ready_to_publish:{job.release_version_id}",
                correlation_id=str(job.id),
                aggregate_type="release",
                aggregate_id=release.id,
                payload={
                    "release_id": str(release.id),
                    "release_version_id": str(job.release_version_id),
                },
                occurred_at=completed_at,
            )
        )
    await session.commit()
    return _job_result(job)


async def fail_derivative_job(
    session: AsyncSession,
    *,
    job_id: UUID,
    worker_id: str,
    expected_lock_version: int,
    error_code: str,
    error_detail: str | None = None,
    now: datetime | None = None,
) -> DerivativeJobResult:
    failed_at = _as_utc(now or datetime.now(UTC))
    worker = _bounded_text(worker_id, "worker id", 200)
    expected = _positive_lock_version(expected_lock_version)
    code = _error_code(error_code)
    detail = _error_detail(error_detail)
    job = await _load_job_locked(session, job_id)
    _require_active_lease(
        job,
        worker_id=worker,
        expected_lock_version=expected,
        now=failed_at,
        allowed_states=(
            DerivativeJobState.CLAIMED,
            DerivativeJobState.PROCESSING,
        ),
    )
    job.state = DerivativeJobState.FAILED
    job.retry_at = None
    job.lease_owner = None
    job.lease_expires_at = None
    job.completed_at = failed_at
    job.last_error_code = code
    job.last_error_detail = detail
    job.lock_version += 1
    _record_job_transition(
        session,
        job=job,
        actor=worker,
        action="derivative.job_failed",
        occurred_at=failed_at,
        detail={"error_code": code, "attempt_count": job.attempt_count},
    )
    await session.commit()
    return _job_result(job)


async def expire_exhausted_derivative_job(
    session: AsyncSession,
    *,
    job_id: UUID,
    expected_lock_version: int,
    now: datetime | None = None,
) -> DerivativeJobResult:
    """Fail an exhausted active job only after its processing lease expires."""

    expired_at = _as_utc(now or datetime.now(UTC))
    expected = _positive_lock_version(expected_lock_version)
    job = await _load_job_locked(session, job_id)
    lease_expires_at = _as_utc(job.lease_expires_at) if job.lease_expires_at is not None else None
    if (
        job.state not in (DerivativeJobState.CLAIMED, DerivativeJobState.PROCESSING)
        or lease_expires_at is None
        or lease_expires_at > expired_at
        or job.attempt_count < job.max_attempts
    ):
        raise DerivativePipelineConflictError("derivative job is not an exhausted expired lease")
    if job.lock_version != expected:
        raise DerivativePipelineConflictError("derivative job lock version is stale")
    job.state = DerivativeJobState.FAILED
    job.retry_at = None
    job.lease_owner = None
    job.lease_expires_at = None
    job.completed_at = expired_at
    job.last_error_code = "execution_lease_expired"
    job.last_error_detail = "Derivative execution exhausted its bounded retry attempts."
    job.lock_version += 1
    _record_job_transition(
        session,
        job=job,
        actor="derivative-lease-recovery",
        action="derivative.job_failed",
        occurred_at=expired_at,
        detail={
            "error_code": "execution_lease_expired",
            "attempt_count": job.attempt_count,
        },
    )
    await session.commit()
    return _job_result(job)


async def cancel_derivative_job(
    session: AsyncSession,
    *,
    job_id: UUID,
    cancelled_by_user_id: UUID,
    expected_lock_version: int,
    reason_code: str,
    now: datetime | None = None,
) -> DerivativeJobResult:
    cancelled_at = _as_utc(now or datetime.now(UTC))
    expected = _positive_lock_version(expected_lock_version)
    code = _error_code(reason_code)
    await _require_active_planner(session, cancelled_by_user_id)
    job = await _load_job_locked(session, job_id)
    if job.state in _TERMINAL_STATES:
        raise DerivativePipelineConflictError("derivative job is already terminal")
    if job.lock_version != expected:
        raise DerivativePipelineConflictError("derivative job lock version is stale")
    job.state = DerivativeJobState.CANCELLED
    job.retry_at = None
    job.lease_owner = None
    job.lease_expires_at = None
    job.completed_at = cancelled_at
    job.last_error_code = code
    job.last_error_detail = "Derivative job cancelled by an authorized reviewer."
    job.lock_version += 1
    _record_job_transition(
        session,
        job=job,
        actor=f"admin:{cancelled_by_user_id}",
        action="derivative.job_cancelled",
        occurred_at=cancelled_at,
        detail={"reason_code": code},
    )
    await session.commit()
    return _job_result(job)


async def _load_and_validate_selections(
    session: AsyncSession,
    task: ReviewTask,
) -> list[ReleaseSelection]:
    rows = list(
        (
            await session.scalars(
                select(ReleaseSelection)
                .where(ReleaseSelection.review_task_id == task.id)
                .order_by(ReleaseSelection.display_order)
            )
        ).all()
    )
    if len(rows) != task.desired_accepted_count or [row.display_order for row in rows] != list(
        range(1, task.desired_accepted_count + 1)
    ):
        raise DerivativePipelineConflictError(
            "completed review task has an incomplete selection snapshot"
        )
    assets = {
        asset.id: asset
        for asset in (
            await session.scalars(
                select(Asset).where(Asset.id.in_([row.asset_id for row in rows])).with_for_update()
            )
        ).all()
    }
    release_version = await session.get(ReleaseVersion, task.release_version_id)
    if release_version is None:
        raise DerivativePipelineConflictError(
            "completed review task release version is unavailable"
        )
    previous_rank = 0
    for selection in rows:
        asset = assets.get(selection.asset_id)
        if (
            selection.release_version_id != task.release_version_id
            or selection.scoring_run_id != task.scoring_run_id
            or selection.ranking_manifest_sha256 != task.ranking_manifest_sha256
            or selection.ranking_rank <= previous_rank
            or asset is None
        ):
            raise DerivativePipelineConflictError(
                "completed review task selection identity changed"
            )
        _validate_available_asset(
            asset,
            expected_kind=AssetKind.RAW_MASTER,
            expected_release_id=release_version.release_id,
            label="selected raw-master source",
        )
        if not _selection_matches_asset(selection, asset):
            raise DerivativePipelineConflictError("selected raw-master storage identity changed")
        previous_rank = selection.ranking_rank
    return rows


async def _load_x_selected_asset_ids(
    session: AsyncSession,
    *,
    task: ReviewTask,
    selections: Sequence[ReleaseSelection],
) -> frozenset[UUID]:
    selected = frozenset(
        (
            await session.scalars(
                select(ReviewXSelection.asset_id).where(ReviewXSelection.review_task_id == task.id)
            )
        ).all()
    )
    accepted = {selection.asset_id for selection in selections}
    if len(selected) > 4 or not selected.issubset(accepted):
        raise DerivativePipelineConflictError(
            "frozen X selections do not match the accepted image set"
        )
    return selected


async def _load_watermark_snapshot(
    session: AsyncSession,
    *,
    release_version_id: UUID,
    watermark_asset_id: UUID | None,
) -> dict[str, Any] | None:
    if watermark_asset_id is None:
        return None
    if await session.get(ReleaseVersion, release_version_id) is None:
        raise DerivativePipelineConflictError("recipe release version is unavailable")
    asset = await session.scalar(
        select(Asset).where(Asset.id == watermark_asset_id).with_for_update()
    )
    if asset is None:
        raise DerivativePipelineNotFoundError("watermark asset was not found")
    _validate_registered_watermark_asset(asset)
    return {
        "asset_id": str(asset.id),
        "storage_backend": asset.storage_backend,
        "storage_bucket": asset.storage_bucket,
        "object_key": _required_asset_text(asset.object_key, "object key"),
        "object_version_id": _required_asset_text(
            asset.object_version_id,
            "object version",
        ),
        "sha256": _required_asset_text(asset.sha256, "SHA-256"),
        "content_type": _required_asset_text(asset.content_type, "content type"),
        "image_format": _required_asset_text(asset.image_format, "image format"),
        "width": _required_asset_int(asset.width, "width"),
        "height": _required_asset_int(asset.height, "height"),
        "byte_size": _required_asset_int(asset.byte_size, "byte size"),
    }


def _validate_registered_watermark_asset(asset: Asset) -> None:
    """Validate a global watermark without weakening release-owned asset checks."""

    if (
        not is_registered_watermark(asset)
        or asset.state != AssetState.AVAILABLE
        or not asset.storage_backend.strip()
        or not asset.storage_bucket.strip()
        or asset.object_key is None
        or not asset.object_key.startswith("watermarks/")
        or asset.object_version_id is None
        or not asset.object_version_id.strip()
        or asset.sha256 is None
        or _SHA256.fullmatch(asset.sha256) is None
        or asset.content_type != "image/png"
        or asset.image_format != "PNG"
        or asset.width is None
        or asset.width <= 0
        or asset.height is None
        or asset.height <= 0
        or asset.byte_size is None
        or asset.byte_size <= 0
        or asset.available_at is None
    ):
        raise DerivativePipelineConflictError(
            "registered watermark asset is unavailable or incomplete"
        )


def _validate_existing_recipe(
    recipe: DerivativeRecipe,
    *,
    release_version_id: UUID,
    recipe_identity: dict[str, Any],
    configuration: dict[str, Any],
    output_targets: tuple[str, ...],
) -> None:
    if (
        recipe.release_version_id != release_version_id
        or recipe.logical_key != canonical_sha256(recipe_identity)
        or recipe.config_sha256 != canonical_sha256(configuration)
        or recipe.configuration != configuration
        or _stored_targets(recipe) != output_targets
        or recipe.expected_output_count != len(output_targets)
    ):
        raise DerivativePipelineConflictError(
            "existing derivative recipe conflicts with its canonical identity"
        )


def _job_request_payload(
    *,
    task: ReviewTask,
    selection: ReleaseSelection,
    recipe: DerivativeRecipe,
    output_targets: tuple[str, ...],
    full_output_byte_budget: int,
) -> dict[str, Any]:
    return {
        "schema": "derivative-job-request/v1",
        "review_task_id": str(task.id),
        "release_version_id": str(selection.release_version_id),
        "release_selection_id": str(selection.id),
        "display_order": selection.display_order,
        "scoring_run_id": str(selection.scoring_run_id),
        "ranking_manifest_sha256": selection.ranking_manifest_sha256,
        "output_targets": list(output_targets),
        "full_output_byte_budget": full_output_byte_budget,
        "source": {
            "asset_id": str(selection.asset_id),
            "storage_backend": selection.source_storage_backend,
            "storage_bucket": selection.source_storage_bucket,
            "object_key": selection.source_object_key,
            "object_version_id": selection.source_object_version_id,
            "sha256": selection.source_sha256,
            "content_type": selection.source_content_type,
            "image_format": selection.source_image_format,
            "width": selection.source_width,
            "height": selection.source_height,
            "byte_size": selection.source_byte_size,
        },
        "recipe": {
            "id": str(recipe.id),
            "logical_key": recipe.logical_key,
            "recipe_version": recipe.recipe_version,
            "config_sha256": recipe.config_sha256,
            "renderer_version": recipe.renderer_version,
            "pillow_version": recipe.pillow_version,
            "output_targets": list(_stored_targets(recipe)),
            "watermark_asset_id": (
                str(recipe.watermark_asset_id) if recipe.watermark_asset_id is not None else None
            ),
            "watermark_sha256": recipe.watermark_sha256,
            "watermark_object_version_id": recipe.watermark_object_version_id,
        },
    }


async def _plan_replay(
    session: AsyncSession,
    *,
    scope: str,
    idempotency_key: str,
    request_sha256: str,
) -> DerivativePlanResult | None:
    record = await session.scalar(
        select(IdempotencyRecord).where(
            IdempotencyRecord.scope == scope,
            IdempotencyRecord.idempotency_key == idempotency_key,
        )
    )
    if record is None:
        return None
    if record.request_sha256 != request_sha256:
        raise DerivativePipelineConflictError(
            "idempotency key was already used for another derivative plan"
        )
    body = record.response_body
    try:
        return DerivativePlanResult(
            review_task_id=UUID(str(body["review_task_id"])),
            recipe_id=UUID(str(body["recipe_id"])),
            release_version_id=UUID(str(body["release_version_id"])),
            job_ids=tuple(UUID(str(value)) for value in body["job_ids"]),
            jobs_created=int(body["jobs_created"]),
            total_jobs=int(body["total_jobs"]),
            replayed=True,
        )
    except (KeyError, TypeError, ValueError):
        raise DerivativePipelineConflictError(
            "derivative plan idempotency response is invalid"
        ) from None


def _plan_response_body(result: DerivativePlanResult) -> dict[str, Any]:
    return {
        "review_task_id": str(result.review_task_id),
        "recipe_id": str(result.recipe_id),
        "release_version_id": str(result.release_version_id),
        "job_ids": [str(job_id) for job_id in result.job_ids],
        "jobs_created": result.jobs_created,
        "total_jobs": result.total_jobs,
    }


async def _load_job_locked(
    session: AsyncSession,
    job_id: UUID,
) -> DerivativeJob:
    job = await session.scalar(
        select(DerivativeJob).where(DerivativeJob.id == job_id).with_for_update()
    )
    if job is None:
        raise DerivativePipelineNotFoundError("derivative job was not found")
    return job


def _require_active_lease(
    job: DerivativeJob,
    *,
    worker_id: str,
    expected_lock_version: int,
    now: datetime,
    allowed_states: tuple[DerivativeJobState, ...],
) -> None:
    lease_expires_at = _as_utc(job.lease_expires_at) if job.lease_expires_at is not None else None
    if (
        job.state not in allowed_states
        or job.lease_owner != worker_id
        or lease_expires_at is None
        or lease_expires_at <= now
    ):
        raise DerivativePipelineLeaseError("derivative job lease is not active for this worker")
    if job.lock_version != expected_lock_version:
        raise DerivativePipelineConflictError("derivative job lock version is stale")


def _record_job_transition(
    session: AsyncSession,
    *,
    job: DerivativeJob,
    actor: str,
    action: str,
    occurred_at: datetime,
    detail: dict[str, Any],
) -> None:
    session.add(
        _audit(
            actor=actor,
            action=action,
            resource_type="derivative_job",
            resource_id=job.id,
            correlation_id=str(job.id),
            detail={
                "state": job.state.value,
                "lock_version": job.lock_version,
                **detail,
            },
            occurred_at=occurred_at,
        )
    )
    session.add(
        _outbox(
            topic=action,
            dedupe_key=f"{action}:{job.id}:{job.lock_version}",
            correlation_id=str(job.id),
            aggregate_type="derivative_job",
            aggregate_id=job.id,
            payload={
                "job_id": str(job.id),
                "state": job.state.value,
                "lock_version": job.lock_version,
                **detail,
            },
            occurred_at=occurred_at,
        )
    )


async def _require_active_planner(
    session: AsyncSession,
    user_id: UUID,
) -> None:
    actor_id = await session.scalar(
        select(AdminUser.id).where(
            AdminUser.id == user_id,
            AdminUser.is_active.is_(True),
            AdminUser.role.in_((AdminRole.OWNER, AdminRole.REVIEWER)),
        )
    )
    if actor_id is None:
        raise DerivativePipelineNotFoundError("authorized derivative planner was not found")


def _validate_available_asset(
    asset: Asset,
    *,
    expected_kind: AssetKind | None,
    expected_release_id: UUID,
    label: str,
) -> None:
    if (
        asset.release_id != expected_release_id
        or (expected_kind is not None and asset.kind != expected_kind)
        or asset.state != AssetState.AVAILABLE
        or not asset.storage_backend.strip()
        or not asset.storage_bucket.strip()
        or asset.object_key is None
        or not asset.object_key.strip()
        or asset.object_version_id is None
        or not asset.object_version_id.strip()
        or asset.sha256 is None
        or _SHA256.fullmatch(asset.sha256) is None
        or asset.content_type is None
        or not asset.content_type.strip()
        or asset.image_format is None
        or not asset.image_format.strip()
        or asset.width is None
        or asset.width <= 0
        or asset.height is None
        or asset.height <= 0
        or asset.byte_size is None
        or asset.byte_size <= 0
        or asset.available_at is None
    ):
        raise DerivativePipelineConflictError(f"{label} is unavailable or incomplete")


def _selection_matches_asset(
    selection: ReleaseSelection,
    asset: Asset,
) -> bool:
    return (
        selection.source_storage_backend == asset.storage_backend
        and selection.source_storage_bucket == asset.storage_bucket
        and selection.source_object_key == asset.object_key
        and selection.source_object_version_id == asset.object_version_id
        and selection.source_sha256 == asset.sha256
        and selection.source_content_type == asset.content_type
        and selection.source_image_format == asset.image_format
        and selection.source_width == asset.width
        and selection.source_height == asset.height
        and selection.source_byte_size == asset.byte_size
        and asset.available_at is not None
        and _as_utc(selection.source_available_at) == _as_utc(asset.available_at)
    )


def _normalize_configuration(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise DerivativePipelineInputError("derivative configuration must be a mapping")
    normalized = dict(value)
    if not normalized:
        raise DerivativePipelineInputError("derivative configuration cannot be empty")
    try:
        serialized = canonical_json_bytes(normalized)
    except (TypeError, ValueError):
        raise DerivativePipelineInputError(
            "derivative configuration is not canonical JSON"
        ) from None
    if len(serialized) > _MAX_CONFIGURATION_BYTES:
        raise DerivativePipelineInputError("derivative configuration is too large")
    return normalized


def _normalize_targets(values: Sequence[str]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise DerivativePipelineInputError("output targets must be a sequence")
    targets = tuple(_normalize_target(value) for value in values)
    if not targets or len(targets) > 20 or len(set(targets)) != len(targets):
        raise DerivativePipelineInputError(
            "output targets must contain between 1 and 20 unique values"
        )
    return tuple(sorted(targets))


def _normalize_target(value: str) -> str:
    if not isinstance(value, str):
        raise DerivativePipelineInputError("derivative target is invalid")
    normalized = value.strip().lower()
    if _TARGET.fullmatch(normalized) is None or normalized not in _SUPPORTED_TARGETS:
        raise DerivativePipelineInputError("derivative target is invalid")
    return normalized


def _stored_targets(recipe: DerivativeRecipe) -> tuple[str, ...]:
    try:
        targets = _normalize_targets(recipe.output_targets)
    except DerivativePipelineInputError:
        raise DerivativePipelineConflictError(
            "stored derivative recipe targets are invalid"
        ) from None
    if len(targets) != recipe.expected_output_count:
        raise DerivativePipelineConflictError("stored derivative recipe output count is invalid")
    return targets


def _job_targets(
    request_payload: object,
    *,
    recipe_targets: tuple[str, ...],
) -> tuple[str, ...]:
    if not isinstance(request_payload, dict):
        raise DerivativePipelineConflictError("stored derivative job request is invalid")
    try:
        targets = _normalize_targets(request_payload["output_targets"])
    except (KeyError, DerivativePipelineInputError):
        raise DerivativePipelineConflictError("stored derivative job targets are invalid") from None
    if not set(targets).issubset(recipe_targets):
        raise DerivativePipelineConflictError(
            "stored derivative job targets exceed the approved recipe"
        )
    return targets


def _bounded_text(value: str, label: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise DerivativePipelineInputError(f"{label} is invalid")
    normalized = value.strip()
    if not normalized or len(normalized) > maximum:
        raise DerivativePipelineInputError(f"{label} is invalid")
    return normalized


def _bounded_int(
    value: int,
    label: str,
    *,
    minimum: int,
    maximum: int,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise DerivativePipelineInputError(f"{label} is invalid")
    if value < minimum or value > maximum:
        raise DerivativePipelineInputError(f"{label} is invalid")
    return value


def _positive_lock_version(value: int) -> int:
    return _bounded_int(
        value,
        "expected lock version",
        minimum=1,
        maximum=2_147_483_647,
    )


def _error_code(value: str) -> str:
    normalized = _bounded_text(value, "error code", 100).lower()
    if _CODE.fullmatch(normalized) is None:
        raise DerivativePipelineInputError("error code is invalid")
    return normalized


def _error_detail(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    if not normalized or len(normalized) > _MAX_ERROR_DETAIL:
        raise DerivativePipelineInputError("error detail is invalid")
    return normalized


def _required_asset_text(value: str | None, label: str) -> str:
    if value is None or not value.strip():
        raise DerivativePipelineConflictError(f"asset {label} is unavailable")
    return value


def _required_asset_int(value: int | None, label: str) -> int:
    if value is None or value <= 0:
        raise DerivativePipelineConflictError(f"asset {label} is unavailable")
    return value


def _optional_string(
    value: dict[str, Any] | None,
    key: str,
) -> str | None:
    if value is None:
        return None
    item = value[key]
    if not isinstance(item, str):
        raise DerivativePipelineConflictError("watermark snapshot is invalid")
    return item


def _optional_int(
    value: dict[str, Any] | None,
    key: str,
) -> int | None:
    if value is None:
        return None
    item = value[key]
    if isinstance(item, bool) or not isinstance(item, int):
        raise DerivativePipelineConflictError("watermark snapshot is invalid")
    return int(item)


def _job_result(job: DerivativeJob) -> DerivativeJobResult:
    return DerivativeJobResult(
        job_id=job.id,
        state=job.state,
        attempt_count=job.attempt_count,
        lock_version=job.lock_version,
        retry_at=job.retry_at,
        completed_at=job.completed_at,
    )


def _output_result(
    output: DerivativeOutput,
    *,
    replayed: bool,
) -> DerivativeOutputResult:
    return DerivativeOutputResult(
        output_id=output.id,
        job_id=output.derivative_job_id,
        target=output.target,
        asset_id=output.asset_id,
        lineage_id=output.asset_lineage_id,
        replayed=replayed,
    )


def _audit(
    *,
    actor: str,
    action: str,
    resource_type: str,
    resource_id: UUID,
    correlation_id: str,
    detail: dict[str, Any],
    occurred_at: datetime,
) -> AuditEvent:
    return AuditEvent(
        actor=actor,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        correlation_id=correlation_id,
        detail=detail,
        occurred_at=occurred_at,
    )


def _outbox(
    *,
    topic: str,
    dedupe_key: str,
    correlation_id: str,
    aggregate_type: str,
    aggregate_id: UUID,
    payload: dict[str, Any],
    occurred_at: datetime,
) -> OutboxEvent:
    return OutboxEvent(
        topic=topic,
        dedupe_key=dedupe_key,
        correlation_id=correlation_id,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        payload=payload,
        status=OutboxStatus.PENDING,
        attempts=0,
        max_attempts=10,
        available_at=occurred_at,
        created_at=occurred_at,
    )


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
