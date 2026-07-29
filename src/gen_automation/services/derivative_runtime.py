"""Bounded, restart-safe execution for durable derivative jobs."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid5

import PIL
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from gen_automation.db.models import (
    Asset,
    DerivativeJob,
    DerivativeOutput,
    Release,
    ReleaseSelection,
    ReleaseVersion,
    ReviewTask,
)
from gen_automation.db.models import (
    DerivativeRecipe as StoredDerivativeRecipe,
)
from gen_automation.domain.canonical import canonical_sha256
from gen_automation.domain.deliverability import (
    DeliverabilityError,
    patreon_full_output_byte_budget,
)
from gen_automation.domain.enums import (
    AssetKind,
    AssetState,
    DerivativeJobState,
    ReleasePhase,
    ReviewTaskState,
)
from gen_automation.services.derivative_isolation import (
    DerivativeIsolationCrashError,
    DerivativeIsolationPolicy,
    DerivativeIsolationProtocolError,
    DerivativeIsolationTimeoutError,
    DerivativeIsolationUnavailableError,
    render_platform_derivatives_isolated,
)
from gen_automation.services.derivative_pipeline import (
    ClaimedDerivativeJob,
    DerivativePipelineConflictError,
    claim_derivative_jobs,
    expire_exhausted_derivative_job,
    fail_derivative_job,
    record_derivative_output,
    retry_derivative_job,
    start_derivative_job,
    succeed_derivative_job,
)
from gen_automation.services.derivatives import (
    DEFAULT_DERIVATIVE_LIMITS,
    DERIVATIVE_RENDERER_VERSION,
    BlurCensor,
    DerivativeBundle,
    DerivativeInputError,
    DerivativeRecipe,
    DerivativeRecipeError,
    DerivativeRenderError,
    DerivativeSafetyLimits,
    DerivativeTarget,
    FullDerivativeSpec,
    JpegEncoding,
    MosaicCensor,
    OutputFormat,
    PngEncoding,
    RelativeRegion,
    RenderedDerivative,
    TeaserFitMode,
    WatermarkPosition,
    WatermarkSpec,
    XTeaserSpec,
    derivative_recipe_sha256,
)
from gen_automation.storage.base import (
    ObjectAlreadyExistsError,
    ObjectMetadata,
    ObjectNotFoundError,
    ObjectStore,
    ObjectStoreError,
    ObjectTooLargeError,
)

_ASSET_NAMESPACE = UUID("4672c99e-c78d-45d4-9bb9-3567363bdb92")
_CONFIG_SCHEMA = "derivative-render-recipe/v1"
_SAFE_ERROR_DETAIL = "Derivative execution failed inside the bounded processing boundary."
_MAX_RECIPE_TARGETS = 20


class DerivativeRuntimeError(Exception):
    """Base error for automatic derivative execution."""


class DerivativeRuntimeContractError(DerivativeRuntimeError):
    """A frozen database or object snapshot violates the execution contract."""


class DerivativeOutputConflictError(DerivativeRuntimeContractError):
    """An immutable output key or database row contains different content."""


@dataclass(frozen=True, slots=True)
class DerivativeExecutionSnapshot:
    job_id: UUID
    release_id: UUID
    release_version_id: UUID
    release_selection_id: UUID
    recipe_id: UUID
    job_lock_version: int
    attempt_count: int
    max_attempts: int
    output_targets: tuple[str, ...]
    full_output_byte_budget: int
    recipe_config_sha256: str
    recipe: DerivativeRecipe
    source_asset_id: UUID
    source_storage_backend: str
    source_storage_bucket: str
    source_object_key: str
    source_object_version_id: str
    source_sha256: str
    source_content_type: str
    source_image_format: str
    source_width: int
    source_height: int
    source_byte_size: int
    watermark_storage_backend: str | None
    watermark_storage_bucket: str | None
    watermark_object_key: str | None
    watermark_object_version_id: str | None
    watermark_sha256: str | None
    watermark_content_type: str | None
    watermark_image_format: str | None
    watermark_width: int | None
    watermark_height: int | None
    watermark_byte_size: int | None


@dataclass(frozen=True, slots=True)
class DerivativeExecutionResult:
    job_id: UUID
    state: DerivativeJobState
    replayed: bool
    outputs_written: int = 0
    outputs_registered: int = 0
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class DerivativeCycleResult:
    recovered_expired_lease: bool = False
    claimed_job: bool = False
    execution: DerivativeExecutionResult | None = None

    @property
    def did_work(self) -> bool:
        return self.recovered_expired_lease or self.claimed_job


type DerivativeRenderer = Callable[
    [
        bytes,
        DerivativeRecipe,
        bytes | None,
        tuple[str, ...],
        DerivativeSafetyLimits,
        DerivativeIsolationPolicy,
    ],
    Awaitable[DerivativeBundle],
]


@dataclass(frozen=True, slots=True)
class _ExecutionFailure:
    code: str
    terminal: bool


def derivative_recipe_configuration(recipe: DerivativeRecipe) -> dict[str, Any]:
    """Return the strict JSON recipe shape accepted by the execution runtime."""

    if not isinstance(recipe, DerivativeRecipe):
        raise DerivativeRecipeError("derivative recipe is invalid")
    return {
        "schema": _CONFIG_SCHEMA,
        "version": recipe.version,
        "background_rgb": list(recipe.background_rgb),
        "full": {
            "output_filename": recipe.full.output_filename,
            "max_width": recipe.full.max_width,
            "max_height": recipe.full.max_height,
            "encoding": _encoding_to_wire(recipe.full.encoding),
        },
        "x_teaser": {
            "output_filename": recipe.x_teaser.output_filename,
            "width": recipe.x_teaser.width,
            "height": recipe.x_teaser.height,
            "fit_mode": recipe.x_teaser.fit_mode.value,
            "allow_upscale": recipe.x_teaser.allow_upscale,
            "encoding": _encoding_to_wire(recipe.x_teaser.encoding),
            "censor": _censor_to_wire(recipe.x_teaser.censor),
        },
        "watermark": (
            {
                "width": recipe.watermark.width,
                "margin": recipe.watermark.margin,
                "opacity": recipe.watermark.opacity,
                "position": recipe.watermark.position.value,
            }
            if recipe.watermark is not None
            else None
        ),
    }


def derivative_output_key(
    snapshot: DerivativeExecutionSnapshot,
    artifact: RenderedDerivative,
) -> str:
    """Build a provider-neutral immutable output key from frozen identities."""

    if artifact.target.value not in snapshot.output_targets:
        raise DerivativeRuntimeContractError(
            "rendered target is not in the frozen derivative recipe"
        )
    return (
        f"derivatives/{snapshot.release_id}/{snapshot.release_version_id}/"
        f"{snapshot.job_id}/{snapshot.recipe_id}-{snapshot.recipe_config_sha256}/"
        f"{snapshot.source_sha256}/{artifact.target.value}/"
        f"{artifact.sha256}.{artifact.extension}"
    )


async def run_derivative_cycle(
    sessions: async_sessionmaker[AsyncSession],
    store: ObjectStore,
    *,
    worker_id: str,
    lease_seconds: int,
    retry_base_seconds: int,
    retry_max_seconds: int,
    limits: DerivativeSafetyLimits = DEFAULT_DERIVATIVE_LIMITS,
    isolation_policy: DerivativeIsolationPolicy | None = None,
    renderer: DerivativeRenderer | None = None,
    now: datetime | None = None,
) -> DerivativeCycleResult:
    """Perform at most one sequential derivative recovery or execution unit."""

    cycle_at = _as_utc(now or datetime.now(UTC))
    recovered = await _recover_one_exhausted_lease(
        sessions,
        now=cycle_at,
    )
    if recovered:
        return DerivativeCycleResult(recovered_expired_lease=True)

    async with sessions() as session:
        claims = await claim_derivative_jobs(
            session,
            worker_id=worker_id,
            limit=1,
            lease_seconds=lease_seconds,
            now=cycle_at,
        )
    if not claims:
        return DerivativeCycleResult()
    execution = await process_claimed_derivative_job(
        sessions,
        store,
        claim=claims[0],
        worker_id=worker_id,
        retry_base_seconds=retry_base_seconds,
        retry_max_seconds=retry_max_seconds,
        limits=limits,
        isolation_policy=isolation_policy,
        renderer=renderer,
        now=cycle_at,
    )
    return DerivativeCycleResult(claimed_job=True, execution=execution)


async def process_claimed_derivative_job(
    sessions: async_sessionmaker[AsyncSession],
    store: ObjectStore,
    *,
    claim: ClaimedDerivativeJob,
    worker_id: str,
    retry_base_seconds: int,
    retry_max_seconds: int,
    limits: DerivativeSafetyLimits = DEFAULT_DERIVATIVE_LIMITS,
    isolation_policy: DerivativeIsolationPolicy | None = None,
    renderer: DerivativeRenderer | None = None,
    now: datetime | None = None,
) -> DerivativeExecutionResult:
    """Render and reconcile every expected output under one durable job lease."""

    operation_at = _as_utc(now or datetime.now(UTC))
    _validate_retry_bounds(retry_base_seconds, retry_max_seconds)
    selected_policy = isolation_policy or DerivativeIsolationPolicy()
    selected_renderer = renderer or _render_isolated

    replay = await _completed_replay(
        sessions,
        claim=claim,
    )
    if replay is not None:
        return replay

    async with sessions() as session:
        started = await start_derivative_job(
            session,
            job_id=claim.job_id,
            worker_id=worker_id,
            expected_lock_version=claim.lock_version,
            now=operation_at,
        )
    lock_version = started.lock_version
    snapshot: DerivativeExecutionSnapshot | None = None
    try:
        async with sessions() as session:
            snapshot = await _load_execution_snapshot(
                session,
                claim=claim,
                worker_id=worker_id,
                expected_lock_version=lock_version,
                now=operation_at,
            )
            await session.rollback()
        execution_limits = replace(
            limits,
            max_full_output_bytes=min(
                limits.max_full_output_bytes,
                snapshot.full_output_byte_budget,
            ),
        )
        source_bytes, watermark_bytes = await _read_inputs(
            store,
            snapshot=snapshot,
            limits=execution_limits,
        )
        bundle = await selected_renderer(
            source_bytes,
            snapshot.recipe,
            watermark_bytes,
            snapshot.output_targets,
            execution_limits,
            selected_policy,
        )
        artifacts = _validate_rendered_bundle(
            snapshot,
            bundle=bundle,
            limits=execution_limits,
        )
        outputs_written = 0
        outputs_registered = 0
        for artifact in artifacts:
            key = derivative_output_key(snapshot, artifact)
            metadata = _output_metadata(snapshot, artifact)
            stored, written = await _write_or_adopt_output(
                store,
                key=key,
                artifact=artifact,
                metadata=metadata,
                limits=execution_limits,
            )
            outputs_written += int(written)
            registered = await _register_output(
                sessions,
                store=store,
                snapshot=snapshot,
                artifact=artifact,
                stored=stored,
                worker_id=worker_id,
                expected_lock_version=lock_version,
                now=operation_at,
            )
            outputs_registered += int(registered)
        async with sessions() as session:
            succeeded = await succeed_derivative_job(
                session,
                job_id=claim.job_id,
                worker_id=worker_id,
                expected_lock_version=lock_version,
                now=operation_at,
            )
        return DerivativeExecutionResult(
            job_id=claim.job_id,
            state=succeeded.state,
            replayed=False,
            outputs_written=outputs_written,
            outputs_registered=outputs_registered,
        )
    except asyncio.CancelledError:
        await _cleanup_cancelled_execution(
            sessions,
            claim=claim,
            snapshot=snapshot,
            worker_id=worker_id,
            expected_lock_version=lock_version,
            retry_base_seconds=retry_base_seconds,
            retry_max_seconds=retry_max_seconds,
            now=operation_at,
        )
        raise
    except Exception as error:
        failure = _classify_failure(error)
        return await _transition_execution_failure(
            sessions,
            claim=claim,
            snapshot=snapshot,
            worker_id=worker_id,
            expected_lock_version=lock_version,
            failure=failure,
            retry_base_seconds=retry_base_seconds,
            retry_max_seconds=retry_max_seconds,
            now=operation_at,
        )


async def _render_isolated(
    source: bytes,
    recipe: DerivativeRecipe,
    watermark: bytes | None,
    targets: tuple[str, ...],
    limits: DerivativeSafetyLimits,
    policy: DerivativeIsolationPolicy,
) -> DerivativeBundle:
    return await render_platform_derivatives_isolated(
        source,
        recipe=recipe,
        watermark_png=watermark,
        targets=targets,
        limits=limits,
        policy=policy,
    )


async def _load_execution_snapshot(
    session: AsyncSession,
    *,
    claim: ClaimedDerivativeJob,
    worker_id: str,
    expected_lock_version: int,
    now: datetime,
) -> DerivativeExecutionSnapshot:
    row = (
        await session.execute(
            select(
                DerivativeJob,
                ReleaseSelection,
                StoredDerivativeRecipe,
                ReleaseVersion,
                Release,
                ReviewTask,
            )
            .join(
                ReleaseSelection,
                ReleaseSelection.id == DerivativeJob.release_selection_id,
            )
            .join(
                StoredDerivativeRecipe,
                StoredDerivativeRecipe.id == DerivativeJob.derivative_recipe_id,
            )
            .join(
                ReleaseVersion,
                ReleaseVersion.id == DerivativeJob.release_version_id,
            )
            .join(Release, Release.id == ReleaseVersion.release_id)
            .join(
                ReviewTask,
                ReviewTask.id == ReleaseSelection.review_task_id,
            )
            .where(DerivativeJob.id == claim.job_id)
            .with_for_update()
        )
    ).one_or_none()
    if row is None:
        raise DerivativeRuntimeContractError("derivative execution snapshot is unavailable")
    job, selection, stored_recipe, release_version, release, review_task = row
    lease_expires_at = _as_utc(job.lease_expires_at) if job.lease_expires_at is not None else None
    if (
        job.release_selection_id != claim.release_selection_id
        or job.derivative_recipe_id != claim.derivative_recipe_id
        or job.request_sha256 != claim.request_sha256
        or job.request_payload != claim.request_payload
        or job.state != DerivativeJobState.PROCESSING
        or job.lease_owner != worker_id
        or lease_expires_at is None
        or lease_expires_at <= now
        or job.lock_version != expected_lock_version
        or job.attempt_count != claim.attempt_count
    ):
        raise DerivativePipelineConflictError(
            "derivative execution lease or claim identity is stale"
        )
    if (
        release.phase != ReleasePhase.RENDERING
        or release.current_version_no != release_version.version_no
        or selection.release_version_id != release_version.id
        or selection.review_task_id != review_task.id
        or review_task.release_version_id != release_version.id
        or review_task.state != ReviewTaskState.COMPLETED
        or stored_recipe.release_version_id != release_version.id
        or job.release_version_id != release_version.id
    ):
        raise DerivativeRuntimeContractError(
            "derivative release version is stale or not renderable"
        )
    recipe_targets = _stored_targets(stored_recipe.output_targets)
    targets = _job_output_targets(job.request_payload, recipe_targets=recipe_targets)
    full_output_byte_budget = _job_full_output_byte_budget(job.request_payload)
    try:
        expected_full_output_byte_budget = patreon_full_output_byte_budget(
            review_task.desired_accepted_count
        )
    except DeliverabilityError:
        raise DerivativeRuntimeContractError(
            "completed review selection count exceeds the Patreon delivery contract"
        ) from None
    if full_output_byte_budget != expected_full_output_byte_budget:
        raise DerivativeRuntimeContractError(
            "stored derivative full-output byte budget conflicts with the completed review"
        )
    if (
        len(recipe_targets) != stored_recipe.expected_output_count
        or len(targets) != job.expected_output_count
        or not set(targets).issubset(recipe_targets)
        or any(target not in {member.value for member in DerivativeTarget} for target in targets)
    ):
        raise DerivativeRuntimeContractError("stored derivative output targets are invalid")
    if canonical_sha256(stored_recipe.configuration) != stored_recipe.config_sha256:
        raise DerivativeRuntimeContractError("stored derivative recipe digest is invalid")
    recipe = _recipe_from_configuration(
        stored_recipe.configuration,
        expected_version=f"derivative-v{stored_recipe.recipe_version}",
    )
    if (
        stored_recipe.renderer_version != DERIVATIVE_RENDERER_VERSION
        or stored_recipe.pillow_version != PIL.__version__
    ):
        raise DerivativeRuntimeContractError(
            "approved derivative runtime versions do not match this controller"
        )
    watermark_present = stored_recipe.watermark_asset_id is not None
    if watermark_present != (recipe.watermark is not None):
        raise DerivativeRuntimeContractError(
            "watermark recipe and immutable object snapshot disagree"
        )
    if DerivativeTarget.X_TEASER.value in targets and not watermark_present:
        raise DerivativeRuntimeContractError("a frozen X teaser job requires an approved watermark")
    _validate_source_snapshot(selection)
    _validate_watermark_snapshot(stored_recipe)
    return DerivativeExecutionSnapshot(
        job_id=job.id,
        release_id=release.id,
        release_version_id=release_version.id,
        release_selection_id=selection.id,
        recipe_id=stored_recipe.id,
        job_lock_version=job.lock_version,
        attempt_count=job.attempt_count,
        max_attempts=job.max_attempts,
        output_targets=targets,
        full_output_byte_budget=full_output_byte_budget,
        recipe_config_sha256=stored_recipe.config_sha256,
        recipe=recipe,
        source_asset_id=selection.asset_id,
        source_storage_backend=selection.source_storage_backend,
        source_storage_bucket=selection.source_storage_bucket,
        source_object_key=selection.source_object_key,
        source_object_version_id=selection.source_object_version_id,
        source_sha256=selection.source_sha256,
        source_content_type=selection.source_content_type,
        source_image_format=selection.source_image_format,
        source_width=selection.source_width,
        source_height=selection.source_height,
        source_byte_size=selection.source_byte_size,
        watermark_storage_backend=stored_recipe.watermark_storage_backend,
        watermark_storage_bucket=stored_recipe.watermark_storage_bucket,
        watermark_object_key=stored_recipe.watermark_object_key,
        watermark_object_version_id=(stored_recipe.watermark_object_version_id),
        watermark_sha256=stored_recipe.watermark_sha256,
        watermark_content_type=stored_recipe.watermark_content_type,
        watermark_image_format=stored_recipe.watermark_image_format,
        watermark_width=stored_recipe.watermark_width,
        watermark_height=stored_recipe.watermark_height,
        watermark_byte_size=stored_recipe.watermark_byte_size,
    )


async def _read_inputs(
    store: ObjectStore,
    *,
    snapshot: DerivativeExecutionSnapshot,
    limits: DerivativeSafetyLimits,
) -> tuple[bytes, bytes | None]:
    _require_store_location(
        store,
        backend=snapshot.source_storage_backend,
        bucket=snapshot.source_storage_bucket,
    )
    source = await store.read_bytes(
        snapshot.source_object_key,
        max_bytes=min(snapshot.source_byte_size, limits.max_master_bytes),
        version_id=snapshot.source_object_version_id,
    )
    _verify_image_bytes(
        source,
        expected_size=snapshot.source_byte_size,
        expected_sha256=snapshot.source_sha256,
        expected_content_type=snapshot.source_content_type,
        expected_format=snapshot.source_image_format,
        label="raw master",
    )
    if DerivativeTarget.X_TEASER.value not in snapshot.output_targets:
        return source, None
    if snapshot.watermark_object_key is None:
        return source, None
    if (
        snapshot.watermark_storage_backend is None
        or snapshot.watermark_storage_bucket is None
        or snapshot.watermark_object_version_id is None
        or snapshot.watermark_sha256 is None
        or snapshot.watermark_content_type is None
        or snapshot.watermark_image_format is None
        or snapshot.watermark_byte_size is None
    ):
        raise DerivativeRuntimeContractError("watermark object snapshot is incomplete")
    _require_store_location(
        store,
        backend=snapshot.watermark_storage_backend,
        bucket=snapshot.watermark_storage_bucket,
    )
    watermark = await store.read_bytes(
        snapshot.watermark_object_key,
        max_bytes=min(snapshot.watermark_byte_size, limits.max_watermark_bytes),
        version_id=snapshot.watermark_object_version_id,
    )
    _verify_image_bytes(
        watermark,
        expected_size=snapshot.watermark_byte_size,
        expected_sha256=snapshot.watermark_sha256,
        expected_content_type=snapshot.watermark_content_type,
        expected_format=snapshot.watermark_image_format,
        label="watermark",
    )
    return source, watermark


def _validate_rendered_bundle(
    snapshot: DerivativeExecutionSnapshot,
    *,
    bundle: DerivativeBundle,
    limits: DerivativeSafetyLimits,
) -> tuple[RenderedDerivative, ...]:
    expected_recipe_sha256 = derivative_recipe_sha256(snapshot.recipe)
    if (
        bundle.source_sha256 != snapshot.source_sha256
        or bundle.recipe_sha256 != expected_recipe_sha256
    ):
        raise DerivativeRuntimeContractError(
            "isolated renderer returned a conflicting bundle identity"
        )
    by_target: dict[str, RenderedDerivative] = {}
    for artifact in bundle.artifacts:
        target = artifact.target.value
        if target in by_target:
            raise DerivativeRuntimeContractError("isolated renderer returned duplicate targets")
        _validate_artifact(snapshot, artifact=artifact, limits=limits)
        by_target[target] = artifact
    if set(snapshot.output_targets) != set(by_target):
        raise DerivativeRuntimeContractError(
            "isolated renderer returned a target outside the frozen job snapshot"
        )
    return tuple(by_target[target] for target in snapshot.output_targets)


def _validate_artifact(
    snapshot: DerivativeExecutionSnapshot,
    *,
    artifact: RenderedDerivative,
    limits: DerivativeSafetyLimits,
) -> None:
    if (
        not 0 < artifact.byte_size <= limits.output_byte_limit(artifact.target)
        or len(artifact.data) != artifact.byte_size
        or hashlib.sha256(artifact.data).hexdigest() != artifact.sha256
        or artifact.width <= 0
        or artifact.height <= 0
        or artifact.width > limits.max_output_width
        or artifact.height > limits.max_output_height
        or artifact.width * artifact.height > limits.max_output_pixels
        or artifact.recipe_sha256 != derivative_recipe_sha256(snapshot.recipe)
        or artifact.lineage.source_sha256 != snapshot.source_sha256
        or artifact.lineage.source_byte_size != snapshot.source_byte_size
        or artifact.lineage.source_format.upper() != snapshot.source_image_format.upper()
        or artifact.lineage.source_width != snapshot.source_width
        or artifact.lineage.source_height != snapshot.source_height
        or artifact.lineage.watermark_sha256
        != (snapshot.watermark_sha256 if artifact.target == DerivativeTarget.X_TEASER else None)
        or artifact.lineage.renderer_version != DERIVATIVE_RENDERER_VERSION
        or artifact.lineage.pillow_version != PIL.__version__
    ):
        raise DerivativeRuntimeContractError(
            "isolated renderer returned conflicting artifact metadata"
        )
    _verify_image_bytes(
        artifact.data,
        expected_size=artifact.byte_size,
        expected_sha256=artifact.sha256,
        expected_content_type=artifact.content_type,
        expected_format=artifact.image_format.value,
        label="derivative output",
    )


async def _write_or_adopt_output(
    store: ObjectStore,
    *,
    key: str,
    artifact: RenderedDerivative,
    metadata: dict[str, str],
    limits: DerivativeSafetyLimits,
) -> tuple[ObjectMetadata, bool]:
    written = True
    try:
        stored = await store.write_bytes_if_absent(
            key=key,
            body=artifact.data,
            content_type=artifact.content_type,
            metadata=metadata,
            max_bytes=limits.max_output_bytes,
        )
    except ObjectAlreadyExistsError:
        written = False
        existing = await store.head(key)
        if existing is None:
            raise ObjectNotFoundError("immutable derivative output is unavailable") from None
        stored = existing
    await _validate_stored_output(
        store,
        stored=stored,
        expected_key=key,
        artifact=artifact,
        metadata=metadata,
        limits=limits,
    )
    return stored, written


async def _validate_stored_output(
    store: ObjectStore,
    *,
    stored: ObjectMetadata,
    expected_key: str,
    artifact: RenderedDerivative,
    metadata: Mapping[str, str],
    limits: DerivativeSafetyLimits,
) -> None:
    if (
        stored.key != expected_key
        or stored.byte_size != artifact.byte_size
        or stored.content_type != artifact.content_type
        or stored.version_id is None
        or not stored.version_id.strip()
        or stored.metadata != dict(metadata)
    ):
        raise DerivativeOutputConflictError(
            "immutable derivative object metadata conflicts with the render"
        )
    payload = await store.read_bytes(
        expected_key,
        max_bytes=min(artifact.byte_size, limits.max_output_bytes),
        version_id=stored.version_id,
    )
    if (
        len(payload) != artifact.byte_size
        or hashlib.sha256(payload).hexdigest() != artifact.sha256
        or not hmac.compare_digest(payload, artifact.data)
    ):
        raise DerivativeOutputConflictError(
            "immutable derivative object bytes conflict with the render"
        )


async def _register_output(
    sessions: async_sessionmaker[AsyncSession],
    *,
    store: ObjectStore,
    snapshot: DerivativeExecutionSnapshot,
    artifact: RenderedDerivative,
    stored: ObjectMetadata,
    worker_id: str,
    expected_lock_version: int,
    now: datetime,
) -> bool:
    if stored.version_id is None:
        raise DerivativeOutputConflictError("immutable derivative object lacks a version identity")
    async with sessions() as session:
        existing = await _load_registered_output(
            session,
            job_id=snapshot.job_id,
            target=artifact.target.value,
        )
        if existing is not None:
            _validate_registered_output(
                existing,
                snapshot=snapshot,
                artifact=artifact,
                stored=stored,
                store=store,
            )
            await session.rollback()
            return False

        asset = await session.scalar(
            select(Asset).where(
                Asset.storage_backend == store.backend,
                Asset.storage_bucket == store.bucket,
                Asset.object_key == stored.key,
            )
        )
        if asset is None:
            asset = Asset(
                id=uuid5(_ASSET_NAMESPACE, f"{store.backend}:{store.bucket}:{stored.key}"),
                release_id=snapshot.release_id,
                generation_job_id=None,
                output_index=None,
                kind=AssetKind.DERIVATIVE,
                state=AssetState.AVAILABLE,
                storage_backend=store.backend,
                storage_bucket=store.bucket,
                object_key=stored.key,
                object_version_id=stored.version_id,
                sha256=artifact.sha256,
                content_type=artifact.content_type,
                image_format=artifact.image_format.value,
                width=artifact.width,
                height=artifact.height,
                byte_size=artifact.byte_size,
                asset_metadata=_asset_metadata(snapshot, artifact),
                available_at=now,
            )
            session.add(asset)
            try:
                await session.flush()
            except IntegrityError:
                await session.rollback()
                existing = await _load_registered_output(
                    session,
                    job_id=snapshot.job_id,
                    target=artifact.target.value,
                )
                if existing is None:
                    raise DerivativeOutputConflictError(
                        "derivative asset registration conflicted"
                    ) from None
                _validate_registered_output(
                    existing,
                    snapshot=snapshot,
                    artifact=artifact,
                    stored=stored,
                    store=store,
                )
                return False
        else:
            _validate_asset(
                asset,
                snapshot=snapshot,
                artifact=artifact,
                stored=stored,
                store=store,
            )
        result = await record_derivative_output(
            session,
            job_id=snapshot.job_id,
            target=artifact.target.value,
            asset_id=asset.id,
            worker_id=worker_id,
            expected_lock_version=expected_lock_version,
            now=now,
        )
        return not result.replayed


async def _load_registered_output(
    session: AsyncSession,
    *,
    job_id: UUID,
    target: str,
) -> tuple[DerivativeOutput, Asset] | None:
    row = (
        await session.execute(
            select(DerivativeOutput, Asset)
            .join(Asset, Asset.id == DerivativeOutput.asset_id)
            .where(
                DerivativeOutput.derivative_job_id == job_id,
                DerivativeOutput.target == target,
            )
        )
    ).one_or_none()
    if row is None:
        return None
    return row[0], row[1]


def _validate_registered_output(
    existing: tuple[DerivativeOutput, Asset],
    *,
    snapshot: DerivativeExecutionSnapshot,
    artifact: RenderedDerivative,
    stored: ObjectMetadata,
    store: ObjectStore,
) -> None:
    output, asset = existing
    if (
        output.release_selection_id != snapshot.release_selection_id
        or output.derivative_recipe_id != snapshot.recipe_id
        or output.source_asset_id != snapshot.source_asset_id
        or output.target != artifact.target.value
        or output.asset_storage_backend != store.backend
        or output.asset_storage_bucket != store.bucket
        or output.asset_object_key != stored.key
        or output.asset_object_version_id != stored.version_id
        or output.asset_sha256 != artifact.sha256
        or output.asset_content_type != artifact.content_type
        or output.asset_image_format != artifact.image_format.value
        or output.asset_width != artifact.width
        or output.asset_height != artifact.height
        or output.asset_byte_size != artifact.byte_size
        or output.lineage_relation != "derivative"
        or output.lineage_recipe_version != snapshot.recipe_config_sha256
    ):
        raise DerivativeOutputConflictError(
            "registered derivative output conflicts with the render"
        )
    _validate_asset(
        asset,
        snapshot=snapshot,
        artifact=artifact,
        stored=stored,
        store=store,
    )


def _validate_asset(
    asset: Asset,
    *,
    snapshot: DerivativeExecutionSnapshot,
    artifact: RenderedDerivative,
    stored: ObjectMetadata,
    store: ObjectStore,
) -> None:
    if (
        asset.release_id != snapshot.release_id
        or asset.kind != AssetKind.DERIVATIVE
        or asset.state != AssetState.AVAILABLE
        or asset.storage_backend != store.backend
        or asset.storage_bucket != store.bucket
        or asset.object_key != stored.key
        or asset.object_version_id != stored.version_id
        or asset.sha256 != artifact.sha256
        or asset.content_type != artifact.content_type
        or asset.image_format != artifact.image_format.value
        or asset.width != artifact.width
        or asset.height != artifact.height
        or asset.byte_size != artifact.byte_size
        or asset.asset_metadata != _asset_metadata(snapshot, artifact)
    ):
        raise DerivativeOutputConflictError("registered derivative asset conflicts with the render")


async def _completed_replay(
    sessions: async_sessionmaker[AsyncSession],
    *,
    claim: ClaimedDerivativeJob,
) -> DerivativeExecutionResult | None:
    async with sessions() as session:
        row = (
            await session.execute(
                select(DerivativeJob, StoredDerivativeRecipe, ReleaseVersion, Release)
                .join(
                    StoredDerivativeRecipe,
                    StoredDerivativeRecipe.id == DerivativeJob.derivative_recipe_id,
                )
                .join(
                    ReleaseVersion,
                    ReleaseVersion.id == DerivativeJob.release_version_id,
                )
                .join(Release, Release.id == ReleaseVersion.release_id)
                .where(DerivativeJob.id == claim.job_id)
            )
        ).one_or_none()
        if row is None:
            raise DerivativeRuntimeContractError("derivative job is unavailable")
        job, recipe, release_version, release = row
        if job.state != DerivativeJobState.SUCCEEDED:
            await session.rollback()
            return None
        if (
            job.release_selection_id != claim.release_selection_id
            or job.derivative_recipe_id != claim.derivative_recipe_id
            or job.request_sha256 != claim.request_sha256
            or job.request_payload != claim.request_payload
            or release.current_version_no != release_version.version_no
            or release.phase not in (ReleasePhase.RENDERING, ReleasePhase.READY_TO_PUBLISH)
        ):
            raise DerivativeOutputConflictError("completed derivative replay identity is invalid")
        expected_targets = _job_output_targets(
            job.request_payload,
            recipe_targets=_stored_targets(recipe.output_targets),
        )
        actual_targets = tuple(
            (
                await session.scalars(
                    select(DerivativeOutput.target)
                    .where(DerivativeOutput.derivative_job_id == job.id)
                    .order_by(DerivativeOutput.target)
                )
            ).all()
        )
        if actual_targets != tuple(sorted(expected_targets)):
            raise DerivativeOutputConflictError("completed derivative outputs are incomplete")
        job_id = job.id
        await session.rollback()
        return DerivativeExecutionResult(
            job_id=job_id,
            state=DerivativeJobState.SUCCEEDED,
            replayed=True,
        )


async def _transition_execution_failure(
    sessions: async_sessionmaker[AsyncSession],
    *,
    claim: ClaimedDerivativeJob,
    snapshot: DerivativeExecutionSnapshot | None,
    worker_id: str,
    expected_lock_version: int,
    failure: _ExecutionFailure,
    retry_base_seconds: int,
    retry_max_seconds: int,
    now: datetime,
) -> DerivativeExecutionResult:
    max_attempts = (
        snapshot.max_attempts
        if snapshot is not None
        else await _load_max_attempts(sessions, claim.job_id)
    )
    terminal = failure.terminal or claim.attempt_count >= max_attempts
    async with sessions() as session:
        if terminal:
            result = await fail_derivative_job(
                session,
                job_id=claim.job_id,
                worker_id=worker_id,
                expected_lock_version=expected_lock_version,
                error_code=failure.code,
                error_detail=_SAFE_ERROR_DETAIL,
                now=now,
            )
        else:
            retry_at = now + timedelta(
                seconds=_retry_delay(
                    attempt=claim.attempt_count,
                    base_seconds=retry_base_seconds,
                    maximum_seconds=retry_max_seconds,
                )
            )
            result = await retry_derivative_job(
                session,
                job_id=claim.job_id,
                worker_id=worker_id,
                expected_lock_version=expected_lock_version,
                retry_at=retry_at,
                error_code=failure.code,
                error_detail=_SAFE_ERROR_DETAIL,
                now=now,
            )
    return DerivativeExecutionResult(
        job_id=claim.job_id,
        state=result.state,
        replayed=False,
        error_code=failure.code,
    )


async def _cleanup_cancelled_execution(
    sessions: async_sessionmaker[AsyncSession],
    *,
    claim: ClaimedDerivativeJob,
    snapshot: DerivativeExecutionSnapshot | None,
    worker_id: str,
    expected_lock_version: int,
    retry_base_seconds: int,
    retry_max_seconds: int,
    now: datetime,
) -> None:
    try:
        await asyncio.shield(
            _transition_execution_failure(
                sessions,
                claim=claim,
                snapshot=snapshot,
                worker_id=worker_id,
                expected_lock_version=expected_lock_version,
                failure=_ExecutionFailure(
                    code="execution_cancelled",
                    terminal=False,
                ),
                retry_base_seconds=retry_base_seconds,
                retry_max_seconds=retry_max_seconds,
                now=now,
            )
        )
    except Exception:
        return


async def _load_max_attempts(
    sessions: async_sessionmaker[AsyncSession],
    job_id: UUID,
) -> int:
    async with sessions() as session:
        value = await session.scalar(
            select(DerivativeJob.max_attempts).where(DerivativeJob.id == job_id)
        )
        await session.rollback()
    if value is None:
        raise DerivativeRuntimeContractError("derivative job is unavailable")
    return int(value)


async def _recover_one_exhausted_lease(
    sessions: async_sessionmaker[AsyncSession],
    *,
    now: datetime,
) -> bool:
    async with sessions() as session:
        job = await session.scalar(
            select(DerivativeJob)
            .where(
                DerivativeJob.state.in_(
                    (DerivativeJobState.CLAIMED, DerivativeJobState.PROCESSING)
                ),
                DerivativeJob.lease_expires_at.is_not(None),
                DerivativeJob.lease_expires_at <= now,
                DerivativeJob.attempt_count >= DerivativeJob.max_attempts,
            )
            .order_by(DerivativeJob.lease_expires_at, DerivativeJob.id)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if job is None:
            await session.rollback()
            return False
        await expire_exhausted_derivative_job(
            session,
            job_id=job.id,
            expected_lock_version=job.lock_version,
            now=now,
        )
        return True


def _classify_failure(error: Exception) -> _ExecutionFailure:
    if isinstance(error, DerivativeOutputConflictError):
        return _ExecutionFailure("output_object_conflict", True)
    if isinstance(error, DerivativeRuntimeContractError):
        return _ExecutionFailure("derivative_contract_invalid", True)
    if isinstance(error, (DerivativeInputError, ObjectTooLargeError)):
        return _ExecutionFailure("derivative_input_invalid", True)
    if isinstance(error, DerivativeRecipeError):
        return _ExecutionFailure("derivative_recipe_invalid", True)
    if isinstance(error, DerivativeRenderError):
        return _ExecutionFailure("derivative_render_invalid", True)
    if isinstance(error, ObjectNotFoundError):
        return _ExecutionFailure("object_version_unavailable", False)
    if isinstance(error, ObjectStoreError):
        return _ExecutionFailure("object_storage_failed", False)
    if isinstance(error, DerivativeIsolationTimeoutError):
        return _ExecutionFailure("renderer_timeout", False)
    if isinstance(error, DerivativeIsolationProtocolError):
        return _ExecutionFailure("renderer_protocol_error", False)
    if isinstance(error, DerivativeIsolationUnavailableError):
        return _ExecutionFailure("renderer_isolation_unavailable", False)
    if isinstance(error, DerivativeIsolationCrashError):
        return _ExecutionFailure("renderer_crash", False)
    if isinstance(error, MemoryError):
        return _ExecutionFailure("renderer_memory_limit", False)
    if isinstance(error, DerivativePipelineConflictError):
        return _ExecutionFailure("derivative_lease_lost", False)
    return _ExecutionFailure("derivative_execution_failed", False)


def _output_metadata(
    snapshot: DerivativeExecutionSnapshot,
    artifact: RenderedDerivative,
) -> dict[str, str]:
    return {
        "sha256": artifact.sha256,
        "release-id": str(snapshot.release_id),
        "release-version-id": str(snapshot.release_version_id),
        "derivative-job-id": str(snapshot.job_id),
        "derivative-recipe-id": str(snapshot.recipe_id),
        "recipe-config-sha256": snapshot.recipe_config_sha256,
        "source-sha256": snapshot.source_sha256,
        "target": artifact.target.value,
        "lineage-sha256": artifact.lineage_sha256,
    }


def _asset_metadata(
    snapshot: DerivativeExecutionSnapshot,
    artifact: RenderedDerivative,
) -> dict[str, Any]:
    return {
        "schema": "derivative-output/v1",
        "derivative_job_id": str(snapshot.job_id),
        "derivative_recipe_id": str(snapshot.recipe_id),
        "release_selection_id": str(snapshot.release_selection_id),
        "source_asset_id": str(snapshot.source_asset_id),
        "source_sha256": snapshot.source_sha256,
        "target": artifact.target.value,
        "recipe_config_sha256": snapshot.recipe_config_sha256,
        "render_recipe_sha256": artifact.recipe_sha256,
        "lineage_sha256": artifact.lineage_sha256,
        "renderer_version": artifact.lineage.renderer_version,
        "pillow_version": artifact.lineage.pillow_version,
    }


def _validate_source_snapshot(selection: ReleaseSelection) -> None:
    if (
        not selection.source_storage_backend.strip()
        or not selection.source_storage_bucket.strip()
        or not selection.source_object_key.strip()
        or not selection.source_object_version_id.strip()
        or not _is_sha256(selection.source_sha256)
        or not selection.source_content_type.strip()
        or not selection.source_image_format.strip()
        or selection.source_width <= 0
        or selection.source_height <= 0
        or selection.source_byte_size <= 0
    ):
        raise DerivativeRuntimeContractError("raw-master source snapshot is incomplete")


def _validate_watermark_snapshot(recipe: StoredDerivativeRecipe) -> None:
    values = (
        recipe.watermark_storage_backend,
        recipe.watermark_storage_bucket,
        recipe.watermark_object_key,
        recipe.watermark_object_version_id,
        recipe.watermark_sha256,
        recipe.watermark_content_type,
        recipe.watermark_image_format,
        recipe.watermark_width,
        recipe.watermark_height,
        recipe.watermark_byte_size,
    )
    if recipe.watermark_asset_id is None:
        if any(value is not None for value in values):
            raise DerivativeRuntimeContractError("watermark snapshot is inconsistent")
        return
    if (
        any(value is None for value in values)
        or not cast(str, recipe.watermark_storage_backend).strip()
        or not cast(str, recipe.watermark_storage_bucket).strip()
        or not cast(str, recipe.watermark_object_key).strip()
        or not cast(str, recipe.watermark_object_version_id).strip()
        or not _is_sha256(cast(str, recipe.watermark_sha256))
        or not cast(str, recipe.watermark_content_type).strip()
        or not cast(str, recipe.watermark_image_format).strip()
        or cast(int, recipe.watermark_width) <= 0
        or cast(int, recipe.watermark_height) <= 0
        or cast(int, recipe.watermark_byte_size) <= 0
    ):
        raise DerivativeRuntimeContractError("watermark snapshot is incomplete")


def _require_store_location(
    store: ObjectStore,
    *,
    backend: str,
    bucket: str,
) -> None:
    if store.backend != backend or store.bucket != bucket:
        raise DerivativeRuntimeContractError(
            "object store does not match the frozen derivative snapshot"
        )


def _verify_image_bytes(
    payload: bytes,
    *,
    expected_size: int,
    expected_sha256: str,
    expected_content_type: str,
    expected_format: str,
    label: str,
) -> None:
    image_format = expected_format.upper()
    expected_types = {
        "JPEG": "image/jpeg",
        "PNG": "image/png",
        "WEBP": "image/webp",
    }
    signature_valid = (
        (image_format == "PNG" and payload.startswith(b"\x89PNG\r\n\x1a\n"))
        or (
            image_format == "JPEG"
            and payload.startswith(b"\xff\xd8\xff")
            and payload.endswith(b"\xff\xd9")
        )
        or (
            image_format == "WEBP"
            and len(payload) >= 12
            and payload.startswith(b"RIFF")
            and payload[8:12] == b"WEBP"
        )
    )
    if (
        len(payload) != expected_size
        or hashlib.sha256(payload).hexdigest() != expected_sha256
        or expected_types.get(image_format) != expected_content_type.lower()
        or not signature_valid
    ):
        raise DerivativeRuntimeContractError(
            f"{label} bytes conflict with the frozen content identity"
        )


def _stored_targets(value: object) -> tuple[str, ...]:
    if (
        not isinstance(value, list)
        or not 1 <= len(value) <= _MAX_RECIPE_TARGETS
        or not all(
            isinstance(target, str) and target and target == target.strip().lower()
            for target in value
        )
        or len(set(value)) != len(value)
    ):
        raise DerivativeRuntimeContractError("stored derivative targets are malformed")
    return tuple(sorted(cast(list[str], value)))


def _job_output_targets(
    request_payload: object,
    *,
    recipe_targets: tuple[str, ...],
) -> tuple[str, ...]:
    if not isinstance(request_payload, dict):
        raise DerivativeRuntimeContractError("derivative job request is malformed")
    targets = _stored_targets(request_payload.get("output_targets"))
    if not set(targets).issubset(recipe_targets):
        raise DerivativeRuntimeContractError("derivative job targets exceed the approved recipe")
    return targets


def _job_full_output_byte_budget(request_payload: object) -> int:
    if not isinstance(request_payload, dict):
        raise DerivativeRuntimeContractError("derivative job request is malformed")
    value = request_payload.get("full_output_byte_budget")
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise DerivativeRuntimeContractError("derivative job full-output byte budget is malformed")
    return value


def _recipe_from_configuration(
    value: object,
    *,
    expected_version: str,
) -> DerivativeRecipe:
    try:
        root = _mapping(
            value,
            keys={
                "schema",
                "version",
                "background_rgb",
                "full",
                "x_teaser",
                "watermark",
            },
        )
        if root["schema"] != _CONFIG_SCHEMA or root["version"] != expected_version:
            raise ValueError
        background = _integer_sequence(
            root["background_rgb"],
            length=3,
            minimum=0,
            maximum=255,
        )
        full_wire = _mapping(
            root["full"],
            keys={"output_filename", "max_width", "max_height", "encoding"},
        )
        teaser_wire = _mapping(
            root["x_teaser"],
            keys={
                "output_filename",
                "width",
                "height",
                "fit_mode",
                "allow_upscale",
                "encoding",
                "censor",
            },
        )
        watermark_wire = root["watermark"]
        watermark = (
            None
            if watermark_wire is None
            else _watermark_from_wire(
                _mapping(
                    watermark_wire,
                    keys={"width", "margin", "opacity", "position"},
                )
            )
        )
        return DerivativeRecipe(
            version=_string(root["version"], maximum=100),
            background_rgb=cast(tuple[int, int, int], background),
            full=FullDerivativeSpec(
                output_filename=_string(
                    full_wire["output_filename"],
                    maximum=120,
                ),
                max_width=_integer(
                    full_wire["max_width"],
                    minimum=1,
                    maximum=16_384,
                ),
                max_height=_integer(
                    full_wire["max_height"],
                    minimum=1,
                    maximum=16_384,
                ),
                encoding=_encoding_from_wire(full_wire["encoding"]),
            ),
            x_teaser=XTeaserSpec(
                output_filename=_string(
                    teaser_wire["output_filename"],
                    maximum=120,
                ),
                width=_integer(
                    teaser_wire["width"],
                    minimum=1,
                    maximum=16_384,
                ),
                height=_integer(
                    teaser_wire["height"],
                    minimum=1,
                    maximum=16_384,
                ),
                fit_mode=TeaserFitMode(_string(teaser_wire["fit_mode"], maximum=20)),
                allow_upscale=_boolean(teaser_wire["allow_upscale"]),
                encoding=_encoding_from_wire(teaser_wire["encoding"]),
                censor=_censor_from_wire(teaser_wire["censor"]),
            ),
            watermark=watermark,
        )
    except (KeyError, TypeError, ValueError):
        raise DerivativeRuntimeContractError("stored derivative configuration is invalid") from None


def _encoding_to_wire(value: JpegEncoding | PngEncoding) -> dict[str, Any]:
    if isinstance(value, JpegEncoding):
        return {"format": OutputFormat.JPEG.value, "quality": value.quality}
    if isinstance(value, PngEncoding):
        return {
            "format": OutputFormat.PNG.value,
            "compress_level": value.compress_level,
        }
    raise DerivativeRecipeError("derivative encoding is invalid")


def _encoding_from_wire(value: object) -> JpegEncoding | PngEncoding:
    mapping = _plain_mapping(value)
    image_format = mapping.get("format")
    if image_format == OutputFormat.JPEG.value:
        _require_keys(mapping, {"format", "quality"})
        return JpegEncoding(quality=_integer(mapping["quality"], minimum=70, maximum=100))
    if image_format == OutputFormat.PNG.value:
        _require_keys(mapping, {"format", "compress_level"})
        return PngEncoding(
            compress_level=_integer(
                mapping["compress_level"],
                minimum=0,
                maximum=9,
            )
        )
    raise ValueError


def _censor_to_wire(value: MosaicCensor | BlurCensor | None) -> dict[str, Any] | None:
    if value is None:
        return None
    result: dict[str, Any] = {
        "mode": value.mode.value,
        "region": {
            "x": value.region.x,
            "y": value.region.y,
            "width": value.region.width,
            "height": value.region.height,
        },
    }
    if isinstance(value, MosaicCensor):
        result["block_size"] = value.block_size
    elif isinstance(value, BlurCensor):
        result["radius"] = value.radius
    else:
        raise DerivativeRecipeError("derivative censor is invalid")
    return result


def _censor_from_wire(value: object) -> MosaicCensor | BlurCensor | None:
    if value is None:
        return None
    mapping = _plain_mapping(value)
    mode = mapping.get("mode")
    region_wire = _mapping(
        mapping.get("region"),
        keys={"x", "y", "width", "height"},
    )
    region = RelativeRegion(
        x=_integer(region_wire["x"], minimum=0, maximum=999_999),
        y=_integer(region_wire["y"], minimum=0, maximum=999_999),
        width=_integer(region_wire["width"], minimum=1, maximum=1_000_000),
        height=_integer(
            region_wire["height"],
            minimum=1,
            maximum=1_000_000,
        ),
    )
    if mode == "mosaic":
        _require_keys(mapping, {"mode", "region", "block_size"})
        return MosaicCensor(
            region=region,
            block_size=_integer(
                mapping["block_size"],
                minimum=2,
                maximum=256,
            ),
        )
    if mode == "blur":
        _require_keys(mapping, {"mode", "region", "radius"})
        return BlurCensor(
            region=region,
            radius=_integer(mapping["radius"], minimum=1, maximum=64),
        )
    raise ValueError


def _watermark_from_wire(value: Mapping[str, Any]) -> WatermarkSpec:
    return WatermarkSpec(
        width=_integer(value["width"], minimum=10_000, maximum=500_000),
        margin=_integer(value["margin"], minimum=0, maximum=100_000),
        opacity=_integer(value["opacity"], minimum=1, maximum=255),
        position=WatermarkPosition(_string(value["position"], maximum=20)),
    )


def _mapping(value: object, *, keys: set[str]) -> Mapping[str, Any]:
    mapping = _plain_mapping(value)
    _require_keys(mapping, keys)
    return mapping


def _plain_mapping(value: object) -> Mapping[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ValueError
    return cast(Mapping[str, Any], value)


def _require_keys(value: Mapping[str, Any], keys: set[str]) -> None:
    if set(value) != keys:
        raise ValueError


def _integer_sequence(
    value: object,
    *,
    length: int,
    minimum: int,
    maximum: int,
) -> tuple[int, ...]:
    if not isinstance(value, (list, tuple)) or len(value) != length:
        raise ValueError
    return tuple(_integer(item, minimum=minimum, maximum=maximum) for item in value)


def _integer(value: object, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError
    return value


def _boolean(value: object) -> bool:
    if not isinstance(value, bool):
        raise ValueError
    return value


def _string(value: object, *, maximum: int) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > maximum:
        raise ValueError
    return value


def _retry_delay(*, attempt: int, base_seconds: int, maximum_seconds: int) -> int:
    _validate_retry_bounds(base_seconds, maximum_seconds)
    exponent = min(max(attempt - 1, 0), 20)
    return int(min(maximum_seconds, base_seconds * (2**exponent)))


def _validate_retry_bounds(base_seconds: int, maximum_seconds: int) -> None:
    if (
        isinstance(base_seconds, bool)
        or isinstance(maximum_seconds, bool)
        or not 1 <= base_seconds <= maximum_seconds <= 86_400
    ):
        raise ValueError("derivative retry bounds are invalid")


def _is_sha256(value: object) -> bool:
    if not isinstance(value, str) or len(value) != 64:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return value == value.lower()


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
